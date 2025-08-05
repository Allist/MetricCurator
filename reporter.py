# -*- coding: utf-8 -*-
"""
reporter.py (최종 완성본)

이 스크립트는 다음 작업을 수행합니다:
1. Grafana API를 호출하여 지정된 대시보드의 모든 Prometheus 패널 쿼리를 가져옵니다.
   - oauth2-proxy와 enforce_domain 문제를 해결하기 위해 'Host' 헤더를 사용합니다.
2. 추출된 모든 쿼리를 Prometheus에 실행하여 메트릭 데이터를 수집합니다.
3. 수집된 데이터를 텍스트 형식으로 가공하여 Google Gemini API에 전달합니다.
4. Gemini가 분석하고 생성한 일일/주간 보고서를 Discord 웹훅을 통해 전송합니다.
"""
import os
import requests
import google.generativeai as genai
from prometheus_api_client import PrometheusConnect
from prometheus_api_client.exceptions import PrometheusApiClientException

# --- 1. 설정: 아래 값들은 환경 변수를 통해 주입됩니다. ---
# (Kubernetes에서는 Secret을 사용하여 관리합니다)

# Prometheus 서버의 내부 서비스 주소
PROMETHEUS_URL = os.environ.get("PROMETHEUS_URL")

# Discord 웹훅 URL
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")

# Google AI Studio에서 발급받은 Gemini API 키
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

# Grafana 접속 정보
GRAFANA_URL = os.environ.get("GRAFANA_URL") # 예: "http://grafana.monitoring.svc.cluster.local"
GRAFANA_API_KEY = os.environ.get("GRAFANA_API_KEY")
GRAFANA_PUBLIC_DOMAIN = os.environ.get("GRAFANA_PUBLIC_DOMAIN") # 예: "grafana.allist.dev"

# 쿼리를 가져올 Grafana 대시보드 UID
DASHBOARD_UID = os.environ.get("DASHBOARD_UID") # 예: "abcdef123"


# --- 2. API 클라이언트 초기화 ---
try:
    if GEMINI_API_KEY:
        genai.configure(api_key=GEMINI_API_KEY)
    prom = PrometheusConnect(url=PROMETHEUS_URL, disable_ssl=True)
except Exception as e:
    print(f"FATAL: API 클라이언트 초기화 중 에러 발생: {e}")
    exit(1)


def get_all_queries_from_grafana(dashboard_uid):
    """Grafana API를 호출하여 대시보드의 모든 Prometheus 쿼리를 가져옵니다."""
    if not all([GRAFANA_URL, GRAFANA_API_KEY, GRAFANA_PUBLIC_DOMAIN, dashboard_uid]):
        print("ERROR: Grafana 관련 환경변수(URL, API_KEY, PUBLIC_DOMAIN, DASHBOARD_UID)가 모두 설정되지 않았습니다.")
        return []

    # 'Host' 헤더를 Grafana의 Public Domain으로 지정하여 리디렉션 문제를 해결합니다.
    # 'Accept' 헤더로 JSON 응답을 명시적으로 요청합니다.
    headers = {
        'Authorization': f'Bearer {GRAFANA_API_KEY}',
        'Accept': 'application/json',
        'Host': GRAFANA_PUBLIC_DOMAIN
    }
    
    api_url = f"{GRAFANA_URL}/api/dashboards/uid/{dashboard_uid}"
    print(f"Grafana API에 요청을 보냅니다 (Host: {GRAFANA_PUBLIC_DOMAIN}): {api_url}")
    
    try:
        response = requests.get(api_url, headers=headers, timeout=10) # 10초 타임아웃
        
        if response.status_code != 200:
            print(f"ERROR: Grafana로부터 에러 응답 코드를 받았습니다: {response.status_code}")
            print(f"응답 내용: {response.text[:500]}...") # 너무 길 수 있으니 일부만 출력
            return []

        dashboard_data = response.json()
        
        queries_with_titles = []
        panels = dashboard_data.get('dashboard', {}).get('panels', [])
        
        for panel in panels:
            panel_title = panel.get('title', '제목 없는 패널')
            datasource_info = panel.get('datasource', {})
            is_prometheus_panel = (isinstance(datasource_info, dict) and datasource_info.get('type') == 'prometheus') or datasource_info is None

            if 'targets' in panel and is_prometheus_panel:
                for target in panel['targets']:
                    if not target.get('hide') and 'expr' in target and target.get('expr'):
                        query = target['expr']
                        # Grafana 변수를 Prometheus가 이해할 수 있는 값으로 치환
                        query = query.replace('$__range', '24h').replace('$__interval', '5m')
                        
                        queries_with_titles.append({'title': panel_title, 'query': query})
                        print(f"  - 발견: [{panel_title}] 패널의 쿼리")
        
        return queries_with_titles
            
    except requests.exceptions.RequestException as e:
        print(f"ERROR: Grafana API 호출 중 네트워크 에러 발생: {e}")
        return []
    except ValueError as e:
        print(f"ERROR: Grafana 응답을 JSON으로 파싱하는 데 실패했습니다: {e}")
        return []


def get_metrics():
    """Grafana에서 가져온 모든 쿼리를 실행하여 메트릭 데이터를 수집하고 텍스트로 만듭니다."""
    all_queries = get_all_queries_from_grafana(DASHBOARD_UID)

    if not all_queries:
        print("WARNING: Grafana에서 쿼리를 가져오지 못했거나, 유효한 Prometheus 패널이 없습니다.")
        return None

    report_data = f"## 📈 Grafana 대시보드 자동 요약 보고서\n(Dashboard: {DASHBOARD_UID})\n\n"
    
    for item in all_queries:
        title = item['title']
        query = item['query']
        report_data += f"### {title}\n"
        
        try:
            print(f"Prometheus에 쿼리 실행 중: [{title}]")
            metrics = prom.custom_query(query=query)
            
            if not metrics:
                report_data += "- (데이터 없음)\n\n"
                continue

            for metric in metrics:
                labels = metric.get('metric', {})
                # 레이블이 너무 많으면 복잡해지므로 주요 레이블만 선택하거나 단순화할 수 있음
                label_str = ", ".join([f'{k}="{v}"' for k, v in labels.items() if k != '__name__'])
                if not label_str:
                    label_str = "Total"
                
                value = float(metric['value'][1])
                report_data += f"- `{label_str}`: **{value:.2f}**\n"
            
            report_data += "\n"

        except PrometheusApiClientException as e:
            print(f"  - 쿼리 실패: {e}")
            report_data += "  - (쿼리 실행 중 에러 발생)\n\n"
        except Exception as e:
            print(f"  - 알 수 없는 에러: {e}")
            report_data += "  - (알 수 없는 에러 발생)\n\n"
            
    return report_data


def generate_report_with_gemini(metrics_text):
    """수집된 메트릭 텍스트를 Gemini API로 보내 분석 리포트를 생성합니다."""
    if not GEMINI_API_KEY:
        print("ERROR: GEMINI_API_KEY가 설정되지 않아 리포트를 생성할 수 없습니다.")
        return "Gemini API 키가 설정되지 않았습니다."

    if not metrics_text:
        return "메트릭 데이터를 수집하지 못해 리포트를 생성할 수 없습니다."
        
    try:
        model = genai.GenerativeModel('gemini-2.5-pro')
        
        prompt = f"""
        당신은 쿠버네티스 홈서버 클러스터를 관리하는 시스템 관리 전문가입니다.
        아래에 제공된 Grafana 대시보드의 데이터를 분석하여 한국어로 된 기술적인 상태 보고서를 작성해주세요.

        보고서는 Discord에 게시될 것이므로, 마크다운 문법을 적극적으로 사용하여 가독성을 높여주세요. (예: **, ###, `, > 등)
        
        보고서에는 다음 내용이 포함되어야 합니다:
        1.  **전체 시스템 상태 요약**: 클러스터가 안정적인지, 주의가 필요한 부분이 있는지 간결하게 요약합니다.
        2.  **주요 메트릭 분석**: 각 항목(CPU, 메모리, 디스크, 네트워크 등)을 해석하고, 평소와 다른 특이사항이나 임계치에 가까운 값이 있는지 언급합니다.
        3.  **잠재적 문제점 및 권장 사항**: 데이터 추세를 기반으로 앞으로 발생할 수 있는 문제(예: 디스크 용량 부족 경고, 특정 워크로드의 과도한 리소스 사용 등)를 지적하고, 필요한 조치나 확인해볼 사항을 제안해주세요.

        --- 데이터 ---
        {metrics_text}
        --- 보고서 시작 ---
        """
        
        response = model.generate_content(prompt)
        return response.text
    except Exception as e:
        print(f"ERROR: Gemini 리포트 생성 중 에러 발생: {e}")
        return f"Gemini API 호출 중 에러가 발생했습니다: {e}"


def send_to_discord(report_content):
    """생성된 리포트를 Discord 웹훅으로 전송합니다."""
    if not DISCORD_WEBHOOK_URL:
        print("WARNING: Discord 웹훅 URL이 설정되지 않아 전송을 건너뜁니다. 아래는 생성된 리포트 내용입니다:")
        print(report_content)
        return
        
    # Discord 메시지 길이 제한 (2000자) 처리
    chunks = [report_content[i:i + 1990] for i in range(0, len(report_content), 1990)]

    for chunk in chunks:
        payload = {"content": chunk}
        try:
            response = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=10)
            response.raise_for_status()
        except requests.exceptions.RequestException as e:
            print(f"ERROR: Discord 전송 중 에러 발생: {e}")


if __name__ == "__main__":
    print("="*50)
    print("자동화된 서버 상태 보고서 생성을 시작합니다...")
    print("="*50)
    
    metrics_data = get_metrics()
    
    if metrics_data:
        print("\n모든 메트릭 데이터 수집 완료. Gemini에게 분석을 요청합니다.")
        gemini_report = generate_report_with_gemini(metrics_data)
        
        print("\nGemini 리포트 생성 완료. Discord로 전송합니다.")
        send_to_discord(gemini_report)
        print("\n보고서 전송 완료.")
    else:
        print("\n메트릭 수집에 실패하여 보고서 생성을 중단합니다.")
        
    print("="*50)
    print("스크립트 실행을 종료합니다.")
    print("="*50)

