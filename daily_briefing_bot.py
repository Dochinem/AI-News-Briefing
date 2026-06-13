import os
import smtplib
import feedparser
import requests
from datetime import datetime
from urllib.parse import quote
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.utils import formataddr


# =========================================================
# 1. 기본 설정
# =========================================================

# 이 파일은 GitHub Actions 사용을 전제로 한 하드코딩 제거 버전입니다.
# Gmail 앱 비밀번호, Gemini API Key, 수신자 목록은 코드에 적지 않고
# GitHub Secrets 또는 로컬 환경변수에서 읽습니다.

# Gmail SMTP 설정
SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))

# 필수 환경변수
# - SMTP_USER: 보내는 Gmail 주소
# - SMTP_PASSWORD: Gmail 앱 비밀번호
# - MAIL_BCC: 실제 수신자 목록. 쉼표로 구분. 예: a@example.com,b@example.com
SMTP_USER = os.environ["SMTP_USER"]
SMTP_PASSWORD = os.environ["SMTP_PASSWORD"]

# 발신자
MAIL_FROM = SMTP_USER

# To 화면에 표시될 주소
# BCC 방식이므로 실제 수신자는 MAIL_BCC에 넣음
MAIL_TO_DISPLAY = os.environ.get("MAIL_TO_DISPLAY", SMTP_USER)

# 실제 수신자 목록
# 서로의 메일주소는 보이지 않음
MAIL_BCC = [
    email.strip()
    for email in os.environ["MAIL_BCC"].split(",")
    if email.strip()
]

# Gemini API Key
# 비어 있으면 AI 요약 없이 기본 템플릿으로 발송됨.
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()

# Gemini 모델명
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash").strip()

# Gemini 사용 여부
USE_GEMINI = True if GEMINI_API_KEY else False

# 카테고리별 최종 기사 수
FINAL_ITEMS_PER_CATEGORY = int(os.environ.get("FINAL_ITEMS_PER_CATEGORY", "3"))

# 검색어별 가져올 기사 수
ITEMS_PER_QUERY = int(os.environ.get("ITEMS_PER_QUERY", "5"))


# =========================================================
# 2. 검색 카테고리 설정
# =========================================================

CATEGORIES = {
    "AI 주요 동향": [
        "AI 뉴스",
        "생성형 AI",
        "AI regulation",
        "AI governance",
    ],
    "AI × 제약·바이오": [
        "제약 AI",
        "바이오 AI",
        "신약개발 AI",
        "pharmaceutical AI",
        "Pharma 4.0",
        "AI drug discovery",
    ],
    "백신·바이오의약품": [
        "백신 개발",
        "백신 생산",
        "바이오의약품",
        "첨단바이오의약품",
        "mRNA 백신",
    ],
    "GMP·품질·Data Integrity": [
        "GMP",
        "의약품 GMP",
        "Data Integrity",
        "데이터 무결성",
        "의약품 품질",
        "밸리데이션",
        "의약품 규제",
    ],
}


# =========================================================
# 3. 점수화 기준
# =========================================================

HIGH_VALUE_TERMS = [
    "fda", "ema", "who", "mfds", "식약처", "질병관리청",
    "ich", "pic/s", "gmp", "data integrity", "데이터 무결성",
    "바이오의약품", "제약", "신약", "백신", "품질", "규제",
    "validation", "밸리데이션", "manufacturing", "제조",
    "임상", "허가", "심사", "안전성", "유효성",
]

AI_TERMS = [
    "ai", "인공지능", "생성형 ai", "chatgpt", "gpt",
    "llm", "machine learning", "머신러닝", "딥러닝",
]

LOW_VALUE_TERMS = [
    "주가", "급등", "폭등", "테마주", "관련주", "광고",
    "연예", "루머", "맛집", "할인", "이벤트",
]

AUTHORITY_TERMS = [
    "fda", "ema", "who", "mfds", "식약처", "질병관리청",
    "ich", "pic/s", "보건복지부", "중기부",
]


# =========================================================
# 4. RSS 수집 함수
# =========================================================

def build_google_news_rss_url(keyword: str) -> str:
    encoded_keyword = quote(keyword)
    return (
        "https://news.google.com/rss/search?"
        f"q={encoded_keyword}&hl=ko&gl=KR&ceid=KR:ko"
    )


def normalize_title(title: str) -> str:
    return (
        title.lower()
        .replace(" ", "")
        .replace("-", "")
        .replace("_", "")
        .replace("·", "")
        .strip()
    )


def fetch_category_news(category_name: str, queries: list[str]) -> list[dict]:
    items = []
    seen_links = set()
    seen_titles = set()

    for query in queries:
        rss_url = build_google_news_rss_url(query)
        feed = feedparser.parse(rss_url)

        for entry in feed.entries[:ITEMS_PER_QUERY]:
            title = entry.get("title", "").strip()
            link = entry.get("link", "").strip()
            source = entry.get("source", {}).get("title", "출처 미상")

            if not title or not link:
                continue

            normalized = normalize_title(title)

            if link in seen_links:
                continue

            if normalized in seen_titles:
                continue

            seen_links.add(link)
            seen_titles.add(normalized)

            item = {
                "category": category_name,
                "query": query,
                "title": title,
                "source": source,
                "link": link,
            }

            item["score"] = score_article(item)
            items.append(item)

    items.sort(key=lambda x: x["score"], reverse=True)
    return items


# =========================================================
# 5. 기사 관련성 점수 계산
# =========================================================

def score_article(item: dict) -> int:
    title = item["title"].lower()
    source = item["source"].lower()
    query = item["query"].lower()
    category = item["category"]

    score = 0

    # 제목에 업무 관련 핵심어 포함
    for term in HIGH_VALUE_TERMS:
        if term.lower() in title:
            score += 3

    # 제목에 AI 관련어 포함
    for term in AI_TERMS:
        if term.lower() in title:
            score += 2

    # 공식기관/규제기관 관련 가점
    for term in AUTHORITY_TERMS:
        if term.lower() in title or term.lower() in source:
            score += 4

    # 저가치 기사 감점
    for term in LOW_VALUE_TERMS:
        if term.lower() in title:
            score -= 5

    has_ai = any(term.lower() in title for term in AI_TERMS)
    has_pharma = any(
        term in title
        for term in ["제약", "바이오", "신약", "의약품", "pharma", "drug", "clinical"]
    )

    # AI × 제약·바이오 조합 가점
    if has_ai and has_pharma:
        score += 6

    # 카테고리별 직접 관련성 가점
    if category == "AI 주요 동향":
        if has_ai:
            score += 3

    elif category == "AI × 제약·바이오":
        if has_ai and has_pharma:
            score += 6
        if any(term in title for term in ["신약", "임상", "제약", "바이오", "drug discovery"]):
            score += 4

    elif category == "백신·바이오의약품":
        if any(term in title for term in ["백신", "바이오의약품", "첨단바이오", "mRNA".lower(), "생산", "개발"]):
            score += 5

    elif category == "GMP·품질·Data Integrity":
        if any(term in title for term in ["gmp", "data integrity", "데이터 무결성", "품질", "밸리데이션", "규제"]):
            score += 6

    # 검색어와 제목이 직접 연결되면 가점
    if query.replace(" ", "") in title.replace(" ", ""):
        score += 2

    return score


# =========================================================
# 6. 전체 뉴스 수집 및 카테고리별 선별
# =========================================================

def collect_selected_news() -> dict:
    selected_by_category = {}

    for category_name, queries in CATEGORIES.items():
        print(f"[수집] {category_name}")
        candidates = fetch_category_news(category_name, queries)

        selected = candidates[:FINAL_ITEMS_PER_CATEGORY]
        selected_by_category[category_name] = selected

        print(f"  후보 {len(candidates)}개 중 {len(selected)}개 선별")

    return selected_by_category


# =========================================================
# 7. Gemini AI 브리핑 생성
# =========================================================

def build_articles_text(selected_by_category: dict) -> str:
    lines = []

    for category, items in selected_by_category.items():
        lines.append(f"\n[{category}]")

        if not items:
            lines.append("- 수집된 기사 없음")
            continue

        for idx, item in enumerate(items, start=1):
            lines.append(
                f"{idx}. {item['title']} / {item['source']} / "
                f"점수 {item['score']} / {item['link']}"
            )

    return "\n".join(lines)


def generate_ai_briefing_with_gemini(selected_by_category: dict) -> str:
    articles_text = build_articles_text(selected_by_category)

    prompt = f"""
다음은 Google News RSS에서 수집한 AI, 제약·바이오, 백신·바이오의약품, GMP·품질 관련 기사 후보입니다.

당신은 백신안전, 의약품 안전, GMP, 규제과학, 교육기획 관점에서 매일 아침 읽을 수 있는 브리핑을 작성해야 합니다.

작성 기준:
1. 과장하지 말 것.
2. 기사 제목과 출처에 근거해 요약할 것.
3. 원문을 확인하지 않은 세부 사실은 단정하지 말 것.
4. 제약·바이오·GMP·교육자료 활용 관점의 시사점을 포함할 것.
5. 각 카테고리별로 2~3문장 이내로 간결하게 정리할 것.
6. 마지막에 교육·홍보자료로 활용 가능한 포인트를 3개 제시할 것.
7. 공식기관 입장처럼 쓰지 말고, 공개자료 기반 브리핑이라는 톤을 유지할 것.

기사 목록:
{articles_text}

출력 형식:

🗞️ 핵심 브리핑
문단 작성

──────────

1. AI 주요 동향
- 요약:
- 실무 시사점:

2. AI × 제약·바이오
- 요약:
- 실무 시사점:

3. 백신·바이오의약품
- 요약:
- 실무 시사점:

4. GMP·품질·Data Integrity
- 요약:
- 실무 시사점:

5. 교육·홍보자료 활용 포인트
- 포인트 1:
- 포인트 2:
- 포인트 3:
"""

    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{GEMINI_MODEL}:generateContent"
        f"?key={GEMINI_API_KEY}"
    )

    payload = {
        "contents": [
            {
                "parts": [
                    {"text": prompt}
                ]
            }
        ],
        "generationConfig": {
            "temperature": 0.3,
            "maxOutputTokens": 1800,
        }
    }

    response = requests.post(url, json=payload, timeout=60)

    if response.status_code != 200:
        raise RuntimeError(
            f"Gemini API 호출 실패: {response.status_code} / {response.text}"
        )

    data = response.json()

    try:
        return data["candidates"][0]["content"]["parts"][0]["text"].strip()
    except Exception:
        raise RuntimeError(f"Gemini 응답 파싱 실패: {data}")


# =========================================================
# 8. AI 없이 기본 브리핑 생성
# =========================================================

def generate_basic_briefing(selected_by_category: dict) -> str:
    lines = []
    lines.append("🗞️ 핵심 브리핑")
    lines.append(
        "오늘 수집된 공개 뉴스 기준으로 AI, 제약·바이오, 백신·바이오의약품, "
        "GMP·품질 관련 이슈를 카테고리별로 정리했습니다."
    )
    lines.append("")
    lines.append("──────────")
    lines.append("")

    for idx, (category, items) in enumerate(selected_by_category.items(), start=1):
        lines.append(f"{idx}. {category}")

        if not items:
            lines.append("- 수집된 주요 기사가 없습니다.")
            lines.append("")
            continue

        for item in items:
            lines.append(f"- {item['title']} · {item['source']}")
            lines.append(f"  관련성 점수: {item['score']}")
            lines.append(f"  검색 키워드: {item['query']}")
            lines.append(f"  링크: {item['link']}")

        lines.append("")

    lines.append("5. 교육·홍보자료 활용 포인트")
    lines.append("- AI와 제약·바이오 산업 변화 사례를 교육자료 도입부로 활용할 수 있습니다.")
    lines.append("- GMP·품질·Data Integrity 관련 기사는 실무형 교육 사례로 전환할 수 있습니다.")
    lines.append("- 백신·바이오의약품 이슈는 산업 동향 및 규제환경 변화 설명 자료로 활용할 수 있습니다.")
    lines.append("")

    return "\n".join(lines)


# =========================================================
# 9. 메일 본문 생성
# =========================================================

def make_email_body(selected_by_category: dict) -> str:
    today = datetime.now().strftime("%Y-%m-%d")

    if USE_GEMINI:
        try:
            briefing = generate_ai_briefing_with_gemini(selected_by_category)
        except Exception as e:
            print("[경고] Gemini 브리핑 생성 실패. 기본 브리핑으로 대체합니다.")
            print(e)
            briefing = generate_basic_briefing(selected_by_category)
    else:
        briefing = generate_basic_briefing(selected_by_category)

    lines = []
    lines.append(f"📰 오늘의 AI·GMP·바이오의약품 이슈 브리핑 ({today})")
    lines.append("")
    lines.append(briefing)
    lines.append("")
    lines.append("──────────")
    lines.append("원문 기사 목록")
    lines.append("")

    for category, items in selected_by_category.items():
        lines.append(f"[{category}]")

        if not items:
            lines.append("- 수집된 기사 없음")
            lines.append("")
            continue

        for idx, item in enumerate(items, start=1):
            lines.append(f"{idx}. {item['title']} · {item['source']}")
            lines.append(f"   - 관련성 점수: {item['score']}")
            lines.append(f"   - 검색 키워드: {item['query']}")
            lines.append(f"   🔗 {item['link']}")
            lines.append("")

    lines.append("──────────")
    lines.append("※ 본 메일은 공개 뉴스 RSS 기반 자동 수집·요약 테스트입니다.")
    lines.append("※ AI 요약은 원문 확인을 대체하지 않으며, 내부 공식자료로 활용 전 원문 검토가 필요합니다.")
    lines.append("※ 기사 제목·출처·링크는 Google News RSS 검색 결과를 기반으로 합니다.")

    return "\n".join(lines)


# =========================================================
# 10. 메일 발송
# =========================================================

def send_email(subject: str, body: str) -> None:
    message = MIMEMultipart()
    message["From"] = formataddr(("AI GMP Briefing", MAIL_FROM))
    message["To"] = formataddr(("AI GMP Briefing", MAIL_TO_DISPLAY))
    message["Subject"] = subject

    message.attach(MIMEText(body, "plain", "utf-8"))

    recipients = [MAIL_TO_DISPLAY] + MAIL_BCC

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.starttls()
        server.login(SMTP_USER, SMTP_PASSWORD)
        server.sendmail(
            MAIL_FROM,
            recipients,
            message.as_string()
        )


# =========================================================
# 11. 실행부
# =========================================================

def main():
    today = datetime.now().strftime("%Y-%m-%d")

    print("========================================")
    print("Daily AI·GMP·Bio Briefing Bot")
    print("========================================")
    print(f"실행일: {today}")
    print(f"Gemini 사용 여부: {USE_GEMINI}")
    print("")

    selected_by_category = collect_selected_news()

    subject = f"[Daily Briefing] AI·GMP·바이오의약품 이슈 브리핑 - {today}"
    body = make_email_body(selected_by_category)

    print("")
    print("[메일 발송 시작]")
    send_email(subject, body)

    print("[메일 발송 완료]")
    print(f"BCC 수신자 수: {len(MAIL_BCC)}명")


if __name__ == "__main__":
    main()