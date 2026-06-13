import os
import re
import smtplib
from difflib import SequenceMatcher
import feedparser
import requests
from datetime import datetime, timedelta, timezone
from html import escape
from urllib.parse import quote
from zoneinfo import ZoneInfo
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

OFFICIAL_ITEMS_PER_AGENCY = 3

KST = ZoneInfo("Asia/Seoul")
BRIEFING_LOOKBACK_HOURS = {
    "morning": 18,
    "afternoon": 6,
}


def get_kst_now() -> datetime:
    return datetime.now(KST)


def get_briefing_period(now: datetime | None = None) -> str:
    now = now or get_kst_now()

    if now.hour < 12:
        return "morning"

    return "afternoon"


def get_briefing_label(period: str) -> str:
    return "오전" if period == "morning" else "오후"


def get_briefing_prompt_context(period: str) -> str:
    if period == "morning":
        return "전날 오후부터 당일 오전까지 확인된 공개 뉴스 중심의 오전 브리핑"

    return (
        "당일 오전 이후 새로 확인된 공개 뉴스 중심의 오후 업데이트 브리핑. "
        "오전 브리핑과 중복될 가능성이 있는 반복 이슈는 가능하면 새로운 관점이 있을 때만 포함"
    )


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

OFFICIAL_RSS_SOURCES = {
    "MFDS": [
        "http://www.mfds.go.kr/www/rss/brd.do?brdId=ntc0003",
        "http://www.mfds.go.kr/www/rss/brd.do?brdId=ntc0004",
    ],
    "EMA": [
        "https://www.ema.europa.eu/en/news.xml",
        "https://www.ema.europa.eu/en/whats-new.xml",
        "https://www.ema.europa.eu/en/inspections.xml",
    ],
}

# FDA/EMA landing pages are not feed XML and are intentionally not parsed:
# - https://www.fda.gov/about-fda/contact-fda/subscribe-podcasts-and-news-feeds
# - https://www.ema.europa.eu/en/news-events/rss-feeds
# FDA feed candidates below returned 404 during direct URL verification, so they are not parsed:
# - https://www.fda.gov/about-fda/contact-fda/stay-informed/rss-feeds/press-releases/rss.xml
# - https://www.fda.gov/about-fda/contact-fda/stay-informed/rss-feeds/medwatch/rss.xml
# - https://www.fda.gov/about-fda/contact-fda/stay-informed/rss-feeds/drugs/rss.xml
# - https://www.fda.gov/about-fda/contact-fda/stay-informed/rss-feeds/biologics/rss.xml
# Add FDA back to OFFICIAL_RSS_SOURCES when a parseable official RSS XML URL is confirmed.


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

OFFICIAL_HIGH_VALUE_TERMS = [
    "gmp", "의약품", "바이오의약품", "백신", "품질", "안전성",
    "허가", "심사", "규제", "data integrity", "validation",
    "manufacturing", "drug", "biologics", "vaccine", "medicine",
    "medicinal product",
]

OFFICIAL_LOW_VALUE_TERMS = [
    "채용", "행사", "공모", "교육", "설명회", "워크숍", "세미나",
    "recruit", "career", "event", "webinar", "workshop",
]

AUTHORITY_TERMS = [
    "fda", "ema", "who", "mfds", "식약처", "질병관리청",
    "ich", "pic/s", "보건복지부", "중기부",
]

SOURCE_PRIORITY = {
    "식약처": 100,
    "질병관리청": 100,
    "FDA": 100,
    "EMA": 100,
    "WHO": 100,
    "약사공론": 80,
    "약업신문": 80,
    "데일리팜": 80,
    "의학신문": 75,
    "메디파나뉴스": 75,
    "뉴스더보이스헬스케어": 75,
    "연합뉴스": 70,
    "머니투데이": 65,
    "한국경제": 65,
    "매일경제": 65,
    "조선비즈": 65,
    "BRIC": 50,
    "v.daum.net": 40,
}


def get_source_priority(source: str) -> int:
    source_text = source or ""
    source_text_lower = source_text.lower()

    for source_name, priority in SOURCE_PRIORITY.items():
        if source_name.lower() in source_text_lower:
            return priority

    return 30


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


def clean_google_news_title(title: str, source: str) -> str:
    cleaned_title = title.strip()
    cleaned_source = source.strip()

    if not cleaned_source or cleaned_source == "출처 미상":
        return cleaned_title

    suffix_pattern = re.compile(rf"\s-\s{re.escape(cleaned_source)}\s*$")

    if suffix_pattern.search(cleaned_title):
        return suffix_pattern.sub("", cleaned_title, count=1).strip()

    return cleaned_title


def get_entry_published_datetime(entry: dict) -> datetime | None:
    published_time = entry.get("published_parsed") or entry.get("updated_parsed")

    if not published_time:
        return None

    return datetime(*published_time[:6], tzinfo=timezone.utc)


def is_entry_in_briefing_window(entry: dict, period: str, now: datetime) -> bool:
    published_at = get_entry_published_datetime(entry)

    if published_at is None:
        return True

    lookback_hours = BRIEFING_LOOKBACK_HOURS[period]
    return published_at >= now.astimezone(timezone.utc) - timedelta(hours=lookback_hours)


def normalize_title_for_similarity(title: str) -> str:
    normalized = title.lower()

    for source_name in SOURCE_PRIORITY:
        normalized = normalized.replace(source_name.lower(), "")

    normalized = re.sub(r"[\s\"'“”‘’\[\]\(\){}<>〈〉《》「」『』·,.:;!?…_\-–—/\\|]", "", normalized)
    return normalized.strip()


def is_similar_issue(title_a: str, title_b: str) -> bool:
    normalized_a = normalize_title_for_similarity(title_a)
    normalized_b = normalize_title_for_similarity(title_b)

    if not normalized_a or not normalized_b:
        return False

    similarity = SequenceMatcher(None, normalized_a, normalized_b).ratio()
    return similarity >= 0.82


def select_representative_article(group: list[dict]) -> dict:
    return max(
        group,
        key=lambda item: (
            item["source_priority"],
            item["score"],
            -item["candidate_order"],
        ),
    )


def group_similar_issues(candidates: list[dict]) -> list[list[dict]]:
    groups = []

    for item in candidates:
        matched_group = None

        for group in groups:
            if any(is_similar_issue(item["title"], grouped_item["title"]) for grouped_item in group):
                matched_group = group
                break

        if matched_group is None:
            groups.append([item])
        else:
            matched_group.append(item)

    return groups


def select_final_articles(candidates: list[dict], limit: int) -> list[dict]:
    issue_groups = group_similar_issues(candidates)
    representatives = [
        select_representative_article(group)
        for group in issue_groups
    ]

    representatives.sort(
        key=lambda item: (
            item["score"] + item["source_priority"],
            item["score"],
            item["source_priority"],
            -item["candidate_order"],
        ),
        reverse=True,
    )

    return representatives[:limit]


def score_official_update(title: str) -> int:
    title_lower = title.lower()
    score = 0

    for term in OFFICIAL_HIGH_VALUE_TERMS:
        if term.lower() in title_lower:
            score += 10

    for term in OFFICIAL_LOW_VALUE_TERMS:
        if term.lower() in title_lower:
            score -= 8

    return score


def fetch_official_updates() -> dict[str, list[dict]]:
    updates_by_agency = {}

    for agency, rss_urls in OFFICIAL_RSS_SOURCES.items():
        print(f"[공식기관 수집] {agency}")
        agency_candidates = []
        seen_links = set()
        seen_titles = set()

        for rss_url in rss_urls:
            try:
                feed = feedparser.parse(rss_url)
            except Exception as e:
                print(f"[경고] 공식기관 RSS 수집 실패: {agency} / {rss_url} / {e}")
                continue

            if getattr(feed, "bozo", False):
                print(f"[경고] 공식기관 RSS 파싱 경고: {agency} / {rss_url}")

            print(f"  RSS URL: {rss_url}")
            print(f"  entries: {len(feed.entries)}")

            for entry in feed.entries:
                # 공식기관 RSS는 오전/오후 브리핑 시간창을 적용하지 않는다.
                # 며칠 전 항목이어도 RSS상 최신이고 관련성이 높으면 표시될 수 있게 한다.
                title = entry.get("title", "").strip()
                link = entry.get("link", "").strip()
                published_at = get_entry_published_datetime(entry)

                if not title or not link:
                    continue

                normalized = normalize_title(title)

                if link in seen_links or normalized in seen_titles:
                    continue

                item = {
                    "category": "공식기관 업데이트",
                    "title": title,
                    "source": agency,
                    "link": link,
                    "published_at": published_at,
                    "official_score": score_official_update(title),
                    "candidate_order": len(agency_candidates),
                }
                agency_candidates.append(item)
                seen_links.add(link)
                seen_titles.add(normalized)

        agency_candidates.sort(
            key=lambda item: (
                item["published_at"] or datetime.min.replace(tzinfo=timezone.utc),
                item["official_score"],
                -item["candidate_order"],
            ),
            reverse=True,
        )

        updates_by_agency[agency] = agency_candidates[:OFFICIAL_ITEMS_PER_AGENCY]
        print(f"  selected: {len(updates_by_agency[agency])}")

    return updates_by_agency


def fetch_category_news(category_name: str, queries: list[str], period: str, now: datetime) -> list[dict]:
    items = []
    seen_links = set()
    seen_titles = set()

    for query in queries:
        rss_url = build_google_news_rss_url(query)
        feed = feedparser.parse(rss_url)

        for entry in feed.entries[:ITEMS_PER_QUERY]:
            if not is_entry_in_briefing_window(entry, period, now):
                continue

            title = entry.get("title", "").strip()
            link = entry.get("link", "").strip()
            source = entry.get("source", {}).get("title", "출처 미상")
            title = clean_google_news_title(title, source)

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
                "candidate_order": len(items),
                "source_priority": get_source_priority(source),
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

def collect_selected_news(period: str, now: datetime) -> dict:
    selected_by_category = {}

    print("[수집] 공식기관 업데이트")
    try:
        official_updates = fetch_official_updates()
    except Exception as e:
        print(f"[경고] 공식기관 업데이트 수집 실패. 빈 섹션으로 계속 진행합니다: {e}")
        official_updates = {
            agency: []
            for agency in OFFICIAL_RSS_SOURCES
        }

    selected_by_category["공식기관 업데이트"] = official_updates
    total_official_updates = sum(len(items) for items in official_updates.values())
    print(f"  공식기관 업데이트 {total_official_updates}개 선별")

    for category_name, queries in CATEGORIES.items():
        print(f"[수집] {category_name}")
        candidates = fetch_category_news(category_name, queries, period, now)

        selected = select_final_articles(candidates, FINAL_ITEMS_PER_CATEGORY)
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

        if category == "공식기관 업데이트":
            for agency, agency_items in items.items():
                lines.append(f"[{agency}]")

                if not agency_items:
                    lines.append("- 수집된 주요 업데이트가 없습니다.")
                    lines.append("")
                    continue

                for idx, item in enumerate(agency_items, start=1):
                    lines.append(f"{idx}. {item['title']} / {item['link']}")

                lines.append("")

            continue

        if not items:
            lines.append("- 수집된 기사 없음")
            continue

        for idx, item in enumerate(items, start=1):
            lines.append(
                f"{idx}. {item['title']} · {item['source']}\n"
                f"   {item['link']}"
            )

    return "\n".join(lines)


def generate_ai_briefing_with_gemini(selected_by_category: dict, period: str) -> str:
    articles_text = build_articles_text(selected_by_category)
    briefing_context = get_briefing_prompt_context(period)

    prompt = f"""
다음은 공식기관 RSS와 Google News RSS에서 수집한 AI, 제약·바이오, 백신·바이오의약품, GMP·품질 관련 기사 후보입니다.

당신은 백신안전, 의약품 안전, GMP, 규제과학, 교육기획 관점에서 정해진 발송 시점에 읽을 수 있는 브리핑을 작성해야 합니다.

이번 브리핑 시점:
{briefing_context}

작성 기준:
1. 과장하지 말 것.
2. 기사 제목과 출처에 근거해 요약할 것.
3. 원문을 확인하지 않은 세부 사실은 단정하지 말 것.
4. 제약·바이오·GMP·교육자료 활용 관점의 시사점을 포함할 것.
5. 각 카테고리별로 2~3문장 이내로 간결하게 정리할 것.
6. 마지막에 교육·홍보자료로 활용 가능한 포인트를 3개 제시할 것.
7. 공식기관 입장처럼 쓰지 말고, 공개자료 기반 브리핑이라는 톤을 유지할 것.
8. 관련성 점수, 검색 키워드, 점수, query라는 표현은 출력하지 말 것.
9. 각 카테고리의 주요 기사에는 기사 제목, 출처, 링크만 포함할 것.
10. 주요 기사 또는 주요 업데이트의 링크 줄에는 실제 URL을 작성할 것.

기사 목록:
{articles_text}

출력 형식:

🗞️ 핵심 브리핑
문단 작성

──────────

1. 공식기관 업데이트
- 요약:
- 실무 시사점:
- 주요 업데이트:
  [MFDS]
  1) 제목
     링크
  2) 제목
     링크
  3) 제목
     링크

  [EMA]
  1) 제목
     링크
  2) 제목
     링크
  3) 제목
     링크

2. AI 주요 동향
- 요약:
- 실무 시사점:
- 주요 기사:
  1) 기사 제목 · 출처
     링크
  2) 기사 제목 · 출처
     링크
  3) 기사 제목 · 출처
     링크

3. AI × 제약·바이오
- 요약:
- 실무 시사점:
- 주요 기사:
  1) 기사 제목 · 출처
     링크
  2) 기사 제목 · 출처
     링크
  3) 기사 제목 · 출처
     링크

4. 백신·바이오의약품
- 요약:
- 실무 시사점:
- 주요 기사:
  1) 기사 제목 · 출처
     링크
  2) 기사 제목 · 출처
     링크
  3) 기사 제목 · 출처
     링크

5. GMP·품질·Data Integrity
- 요약:
- 실무 시사점:
- 주요 기사:
  1) 기사 제목 · 출처
     링크
  2) 기사 제목 · 출처
     링크
  3) 기사 제목 · 출처
     링크

6. 교육·홍보자료 활용 포인트
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
            "maxOutputTokens": 3200,
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

    for idx, (category, items) in enumerate(selected_by_category.items(), start=1):
        lines.append(f"{idx}. {category}")

        if category == "공식기관 업데이트":
            for agency, agency_items in items.items():
                lines.append(f"[{agency}]")

                if not agency_items:
                    lines.append("- 수집된 주요 업데이트가 없습니다.")
                    lines.append("")
                    continue

                for item in agency_items:
                    lines.append(f"- {item['title']}")
                    lines.append(f"  {item['link']}")

                lines.append("")

            continue

        if not items:
            lines.append("- 수집된 주요 기사가 없습니다.")
            lines.append("")
            continue

        for item in items:
            lines.append(f"- {item['title']} · {item['source']}")
            lines.append(f"  {item['link']}")

        lines.append("")

    return "\n".join(lines)


# =========================================================
# 9. HTML 메일 본문 생성
# =========================================================

CATEGORY_TITLE_STYLE = (
    "font-size: 18px; "
    "font-weight: bold; "
    "margin-top: 24px; "
    "margin-bottom: 10px;"
)

ARTICLE_BLOCK_STYLE = "margin-bottom: 14px;"
ARTICLE_TITLE_STYLE = "font-weight: 600;"
AGENCY_TITLE_STYLE = "font-weight: bold; margin-top: 12px; margin-bottom: 8px;"


def make_article_html(title: str, source: str, link: str, show_source: bool = True) -> str:
    safe_title = escape(title)
    safe_source = escape(source)
    safe_link = escape(link, quote=True)
    title_text = f"{safe_title} · {safe_source}" if show_source else safe_title

    return (
        f'<div style="{ARTICLE_BLOCK_STYLE}">'
        f'<div style="{ARTICLE_TITLE_STYLE}">{title_text}</div>'
        f'<div><a href="{safe_link}">기사 원문 보기</a></div>'
        "</div>"
    )


def make_basic_briefing_html(selected_by_category: dict) -> str:
    blocks = []

    for idx, (category, items) in enumerate(selected_by_category.items(), start=1):
        blocks.append(
            f'<div style="{CATEGORY_TITLE_STYLE}">{idx}. {escape(category)}</div>'
        )

        if category == "공식기관 업데이트":
            for agency, agency_items in items.items():
                blocks.append(f'<div style="{AGENCY_TITLE_STYLE}">[{escape(agency)}]</div>')

                if not agency_items:
                    blocks.append('<div style="margin-bottom: 14px;">- 수집된 주요 업데이트가 없습니다.</div>')
                    continue

                for item in agency_items:
                    blocks.append(
                        make_article_html(
                            item["title"],
                            item["source"],
                            item["link"],
                            show_source=False,
                        )
                    )

            continue

        if not items:
            blocks.append('<div style="margin-bottom: 14px;">- 수집된 주요 기사가 없습니다.</div>')
            continue

        for item in items:
            blocks.append(
                make_article_html(
                    item["title"],
                    item["source"],
                    item["link"],
                )
            )

    return "\n".join(blocks)


def make_official_updates_html(official_updates: dict[str, list[dict]], section_number: int = 1) -> str:
    blocks = [
        f'<div style="{CATEGORY_TITLE_STYLE}">{section_number}. 공식기관 업데이트</div>'
    ]

    for agency, agency_items in official_updates.items():
        blocks.append(f'<div style="{AGENCY_TITLE_STYLE}">[{escape(agency)}]</div>')

        if not agency_items:
            blocks.append('<div style="margin-bottom: 14px;">- 수집된 주요 업데이트가 없습니다.</div>')
            continue

        for item in agency_items:
            blocks.append(
                make_article_html(
                    item["title"],
                    item["source"],
                    item["link"],
                    show_source=False,
                )
            )

    return "\n".join(blocks)


def is_category_heading(line: str) -> bool:
    categories = ["공식기관 업데이트", *CATEGORIES.keys()]

    return any(
        line == f"{idx}. {category}"
        for idx, category in enumerate(categories, start=1)
    )


def make_text_line_html(line: str) -> str:
    safe_line = escape(line)

    if line == "🗞️ 핵심 브리핑":
        return (
            '<div style="font-size: 20px; font-weight: bold; '
            'margin-top: 4px; margin-bottom: 12px;">'
            f"{safe_line}</div>"
        )

    if is_category_heading(line):
        return f'<div style="{CATEGORY_TITLE_STYLE}">{safe_line}</div>'

    if re.fullmatch(r"\[[A-Za-z0-9가-힣·\s]+\]", line):
        return f'<div style="{AGENCY_TITLE_STYLE}">{safe_line}</div>'

    if set(line) == {"─"}:
        return '<hr style="border: 0; border-top: 1px solid #dddddd; margin: 22px 0;">'

    return f'<div style="margin-bottom: 8px; line-height: 1.6;">{safe_line}</div>'


def make_ai_briefing_html(briefing: str) -> str:
    blocks = []
    pending_article_title = None
    url_pattern = re.compile(r"^https?://\S+$")
    article_pattern = re.compile(r"^\d+\)\s+(.+)$")

    for raw_line in briefing.splitlines():
        line = raw_line.strip()

        if not line:
            continue

        article_match = article_pattern.match(line)
        if article_match:
            if pending_article_title:
                blocks.append(
                    f'<div style="{ARTICLE_BLOCK_STYLE}">'
                    f'<div style="{ARTICLE_TITLE_STYLE}">{escape(pending_article_title)}</div>'
                    "</div>"
                )
            pending_article_title = article_match.group(1).strip()
            continue

        if url_pattern.match(line):
            safe_link = escape(line, quote=True)
            if pending_article_title:
                blocks.append(
                    f'<div style="{ARTICLE_BLOCK_STYLE}">'
                    f'<div style="{ARTICLE_TITLE_STYLE}">{escape(pending_article_title)}</div>'
                    f'<div><a href="{safe_link}">기사 원문 보기</a></div>'
                    "</div>"
                )
                pending_article_title = None
            else:
                blocks.append(f'<div style="{ARTICLE_BLOCK_STYLE}"><a href="{safe_link}">기사 원문 보기</a></div>')
            continue

        if pending_article_title:
            blocks.append(
                f'<div style="{ARTICLE_BLOCK_STYLE}">'
                f'<div style="{ARTICLE_TITLE_STYLE}">{escape(pending_article_title)}</div>'
                "</div>"
            )
            pending_article_title = None

        blocks.append(make_text_line_html(line))

    if pending_article_title:
        blocks.append(
            f'<div style="{ARTICLE_BLOCK_STYLE}">'
            f'<div style="{ARTICLE_TITLE_STYLE}">{escape(pending_article_title)}</div>'
            "</div>"
        )

    return "\n".join(blocks)


def remove_ai_official_section(briefing: str) -> str:
    lines = briefing.splitlines()
    filtered_lines = []
    skipping_official_section = False

    for line in lines:
        stripped = line.strip()

        if stripped == "1. 공식기관 업데이트":
            skipping_official_section = True
            continue

        if skipping_official_section and re.match(r"^2\.\s+", stripped):
            skipping_official_section = False

        if not skipping_official_section:
            filtered_lines.append(line)

    return "\n".join(filtered_lines).strip()


# =========================================================
# 10. 메일 본문 생성
# =========================================================

def make_email_body(selected_by_category: dict, period: str, today: str) -> str:
    briefing_label = get_briefing_label(period)
    used_gemini = False

    if USE_GEMINI:
        try:
            briefing = generate_ai_briefing_with_gemini(selected_by_category, period)
            used_gemini = True
        except Exception as e:
            print("[경고] Gemini 브리핑 생성 실패. 기본 브리핑으로 대체합니다.")
            print(e)
            briefing = generate_basic_briefing(selected_by_category)
    else:
        briefing = generate_basic_briefing(selected_by_category)

    if used_gemini:
        official_html = make_official_updates_html(
            selected_by_category.get("공식기관 업데이트", {}),
            section_number=1,
        )
        briefing_html = "\n".join(
            [
                official_html,
                make_ai_briefing_html(remove_ai_official_section(briefing)),
            ]
        )
    else:
        briefing_html = make_basic_briefing_html(selected_by_category)

    return f"""<!doctype html>
<html>
  <body style="margin: 0; padding: 0; background-color: #ffffff;">
    <div style="font-family: Arial, 'Apple SD Gothic Neo', 'Malgun Gothic', sans-serif; color: #222222; font-size: 14px; line-height: 1.6;">
      <div style="font-size: 22px; font-weight: bold; margin-bottom: 18px;">
        📰 {escape(briefing_label)} AI·GMP·바이오의약품 이슈 브리핑 ({escape(today)})
      </div>
      {briefing_html}
      <hr style="border: 0; border-top: 1px solid #dddddd; margin: 24px 0 14px;">
      <div style="font-size: 12px; color: #555555; line-height: 1.7;">
        <div>※ AI 요약은 원문 확인을 대체하지 않으며, 내부 공식자료로 활용 전 원문 검토가 필요합니다.</div>
        <div>※ 기사 제목·출처·링크는 Google News RSS 검색 결과를 기반으로 합니다.</div>
      </div>
    </div>
  </body>
</html>"""


# =========================================================
# 11. 메일 발송
# =========================================================

def send_email(subject: str, body: str) -> None:
    message = MIMEMultipart()
    message["From"] = formataddr(("IRIS", MAIL_FROM))
    message["To"] = formataddr(("IRIS", MAIL_TO_DISPLAY))
    message["Subject"] = subject

    message.attach(MIMEText(body, "html", "utf-8"))

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
# 12. 실행부
# =========================================================

def main():
    now = get_kst_now()
    today = now.strftime("%Y-%m-%d")
    period = get_briefing_period(now)
    briefing_label = get_briefing_label(period)

    print("========================================")
    print("Daily AI·GMP·Bio Briefing Bot")
    print("========================================")
    print(f"실행일: {today}")
    print(f"브리핑 구분: {briefing_label}")
    print(f"Gemini 사용 여부: {USE_GEMINI}")
    print("")

    selected_by_category = collect_selected_news(period, now)

    if period == "morning":
        subject = f"[Morning Briefing] {today}, AI·GMP·바이오의약품 이슈"
    else:
        subject = f"[Afternoon Briefing] {today}, AI·GMP·바이오의약품 이슈"

    body = make_email_body(selected_by_category, period, today)

    print("")
    print("[메일 발송 시작]")
    send_email(subject, body)

    print("[메일 발송 완료]")
    print(f"BCC 수신자 수: {len(MAIL_BCC)}명")


if __name__ == "__main__":
    main()
