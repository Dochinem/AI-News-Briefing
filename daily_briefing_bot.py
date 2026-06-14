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
# GitHub Secrets의 GEMINI_MODEL도 gemini-2.5-flash로 변경해야 함.
# 모델이 폐기되면 Gemini API가 404를 반환할 수 있음.
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash").strip() or "gemini-2.5-flash"

# Gemini 사용 여부
USE_GEMINI = True if GEMINI_API_KEY else False

# 카테고리별 최종 기사 수
FINAL_ITEMS_PER_CATEGORY = int(os.environ.get("FINAL_ITEMS_PER_CATEGORY", "3"))

# 검색어별 가져올 기사 수
ITEMS_PER_QUERY = int(os.environ.get("ITEMS_PER_QUERY", "10"))

OFFICIAL_ITEMS_PER_AGENCY = 3

KST = ZoneInfo("Asia/Seoul")
BRIEFING_LOOKBACK_HOURS = {
    "morning": 18,
    "afternoon": 6,
}
RELAXED_CATEGORY_LOOKBACK_HOURS = 72


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
        "백신",
        "백신 개발",
        "백신 생산",
        "백신 제조",
        "바이오의약품",
        "첨단바이오의약품",
        "항체의약품",
        "세포치료제",
        "유전자치료제",
        "mRNA 백신",
        "CDMO 바이오의약품",
        "바이오시밀러",
        "바이오의약품 허가",
        "바이오의약품 품질",
        "vaccine development",
        "vaccine manufacturing",
        "biologics",
        "biopharmaceutical",
        "biosimilar",
        "cell therapy",
        "gene therapy",
        "mRNA vaccine",
        "biologics manufacturing",
        "biopharma CDMO",
    ],
    "GMP·품질·Data Integrity": [
        "의약품 GMP",
        "GMP",
        "의약품 품질",
        "제약 품질",
        "제약 밸리데이션",
        "밸리데이션",
        "Data Integrity",
        "데이터 무결성",
        "제조소 실사",
        "의약품 제조관리",
        "의약품 품질관리",
        "GMP 실사",
        "GMP 위반",
        "의약품 회수",
        "품질 부적합",
        "경고서",
        "규제기관 실사",
        "pharmaceutical GMP",
        "GMP inspection",
        "GMP compliance",
        "data integrity",
        "pharmaceutical quality",
        "quality management system",
        "validation",
        "computer system validation",
        "FDA warning letter GMP",
        "GMP deficiencies",
        "manufacturing quality",
        "drug recall quality",
    ],
}

CRITICAL_CATEGORY_FALLBACK_QUERIES = {
    "백신·바이오의약품": [
        "바이오의약품 OR 백신",
        "바이오시밀러 OR 항체의약품",
        "첨단바이오의약품 OR 세포치료제 OR 유전자치료제",
        "vaccine OR biologics OR biosimilar",
    ],
    "GMP·품질·Data Integrity": [
        "GMP OR 의약품 품질",
        "데이터 무결성 OR Data Integrity",
        "밸리데이션 OR validation",
        "GMP 실사 OR GMP inspection",
        "pharmaceutical quality OR GMP compliance",
    ],
}

OFFICIAL_RSS_SOURCES = {
    "MFDS": [
        "http://www.mfds.go.kr/www/rss/brd.do?brdId=ntc0003",
        "http://www.mfds.go.kr/www/rss/brd.do?brdId=ntc0004",
        "https://www.mfds.go.kr/www/rss/brd.do?brdId=ntc0003",
        "https://www.mfds.go.kr/www/rss/brd.do?brdId=ntc0004",
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

MFDS_FALLBACK_QUERIES = [
    "site:mfds.go.kr 식약처 의약품",
    "site:mfds.go.kr 식약처 바이오의약품",
    "site:mfds.go.kr 식약처 GMP",
    "site:mfds.go.kr 식약처 백신",
    "site:mfds.go.kr 식약처 첨단바이오의약품",
]


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
    "면접전형", "직원 채용", "공무원지침서", "상세보기", "고시 제",
]

EMA_LOW_VALUE_TERMS = [
    "human medicines european public assessment report",
    "epar",
    "document revision",
    "status update",
    "assessment report",
    "public assessment report",
    "revision",
    "status: authorised",
    "date of authorisation",
]

EMA_HIGH_VALUE_TERMS = [
    "news", "regulatory", "safety", "inspection", "gmp",
    "medicine", "vaccine", "biologics", "public health",
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


def is_entry_in_lookback_window(entry: dict, now: datetime, lookback_hours: int | None) -> bool:
    if lookback_hours is None:
        return True

    published_at = get_entry_published_datetime(entry)

    if published_at is None:
        return True

    return published_at >= now.astimezone(timezone.utc) - timedelta(hours=lookback_hours)


def normalize_title_for_similarity(title: str) -> str:
    return normalize_title_for_dedup(title)


def normalize_title_for_dedup(title: str) -> str:
    normalized = title.lower()

    for source_name in SOURCE_PRIORITY:
        normalized = normalized.replace(source_name.lower(), "")

    normalized = re.sub(r"\s-\s[^\-]+$", "", normalized)
    normalized = normalized.replace("롯데바이오로직스", "롯데바이오")
    normalized = normalized.replace("완공", "준공")
    normalized = normalized.replace("준공식", "준공")
    normalized = normalized.replace("…", "")
    normalized = normalized.replace("...", "")

    removable_terms = [
        "관련 기사", "관련기사", "단독", "속보", "종합", "영상",
        "공식", "발표", "관련", "대해", "대한", "으로", "에서",
        "하고", "하며", "했다", "한다",
    ]
    for term in removable_terms:
        normalized = normalized.replace(term, "")

    normalized = re.sub(r"[\s\"'“”‘’\[\]\(\){}<>〈〉《》「」『』·,.:;!?…_\-–—/\\|]", "", normalized)
    return normalized.strip()


def extract_issue_tokens(title: str) -> set[str]:
    normalized = title.lower()
    normalized = re.sub(r"\s-\s[^\-]+$", "", normalized)
    normalized = normalized.replace("롯데바이오로직스", "롯데바이오")
    normalized = normalized.replace("완공", "준공")
    normalized = normalized.replace("준공식", "준공")
    tokens = set(re.findall(r"[가-힣A-Za-z0-9]+", normalized))

    stopwords = {
        "관련", "기사", "공식", "발표", "단독", "속보", "종합",
        "오늘", "이번", "대한", "대해", "으로", "에서",
    }
    return {token for token in tokens if token not in stopwords and len(token) > 1}


def has_same_core_event(title_a: str, title_b: str) -> bool:
    tokens_a = extract_issue_tokens(title_a)
    tokens_b = extract_issue_tokens(title_b)
    shared = tokens_a & tokens_b

    company_terms = {"롯데바이오", "삼성바이오", "셀트리온", "sk바이오", "유한양행"}
    location_terms = {"송도", "오송", "대전", "인천", "공장", "1공장", "2공장"}
    event_terms = {"준공", "착공", "허가", "승인", "완공", "생산", "출시", "계약"}

    has_company = bool(shared & company_terms)
    has_location = bool(shared & location_terms)
    has_event = bool((tokens_a & event_terms) and (tokens_b & event_terms))

    return has_company and has_location and has_event


def is_same_issue(title_a: str, title_b: str) -> bool:
    normalized_a = normalize_title_for_dedup(title_a)
    normalized_b = normalize_title_for_dedup(title_b)

    if not normalized_a or not normalized_b:
        return False

    similarity = SequenceMatcher(None, normalized_a, normalized_b).ratio()
    if similarity >= 0.78:
        return True

    tokens_a = extract_issue_tokens(title_a)
    tokens_b = extract_issue_tokens(title_b)
    union = tokens_a | tokens_b
    token_overlap = len(tokens_a & tokens_b) / len(union) if union else 0

    return token_overlap >= 0.45 or has_same_core_event(title_a, title_b)


def is_similar_issue(title_a: str, title_b: str) -> bool:
    return is_same_issue(title_a, title_b)


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


def select_final_articles(candidates: list[dict], limit: int, category_name: str = "") -> list[dict]:
    issue_groups = group_similar_issues(candidates)
    representatives = [
        select_representative_article(group)
        for group in issue_groups
    ]

    if category_name:
        print(f"[중복 제거] {category_name}: {len(candidates)}개 → {len(representatives)}개")

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


def get_official_feed_priority(agency: str, rss_url: str) -> int:
    if agency == "EMA":
        if rss_url.endswith("/news.xml"):
            return 30
        if rss_url.endswith("/inspections.xml"):
            return 25
        if rss_url.endswith("/whats-new.xml"):
            return 10

    return 0


def score_official_update(title: str, agency: str = "", rss_url: str = "") -> int:
    title_lower = title.lower()
    score = 0

    for term in OFFICIAL_HIGH_VALUE_TERMS:
        if term.lower() in title_lower:
            score += 10

    for term in OFFICIAL_LOW_VALUE_TERMS:
        if term.lower() in title_lower:
            score -= 8

    if agency == "EMA":
        for term in EMA_HIGH_VALUE_TERMS:
            if term.lower() in title_lower:
                score += 8

        for term in EMA_LOW_VALUE_TERMS:
            if term.lower() in title_lower:
                score -= 25

    score += get_official_feed_priority(agency, rss_url)

    return score


def is_low_quality_official_update(title: str, agency: str) -> bool:
    title_lower = title.lower()

    if agency == "MFDS":
        return any(term.lower() in title_lower for term in OFFICIAL_LOW_VALUE_TERMS)

    return False


def parse_mfds_rss_with_diagnostics(rss_url: str):
    print("[MFDS RSS 진단]")
    print(f"URL: {rss_url}")

    try:
        response = requests.get(
            rss_url,
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=30,
        )
    except Exception as e:
        print(f"status_code: request_failed")
        print(f"content_type: unknown")
        print(f"preview: {e}")
        return feedparser.parse("")

    content_type = response.headers.get("content-type", "")
    preview = response.text[:300].replace("\n", " ").replace("\r", " ").strip()
    feed = feedparser.parse(response.content)

    print(f"status_code: {response.status_code}")
    print(f"content_type: {content_type}")
    print(f"preview: {preview}")
    print(f"bozo: {bool(getattr(feed, 'bozo', False))}")
    print(f"entries: {len(feed.entries)}")

    if "xml" not in content_type.lower() and not response.text.lstrip().startswith("<?xml"):
        print("[경고] MFDS RSS가 XML이 아닌 응답을 반환했습니다. HTML/차단/리다이렉트/오류 페이지일 수 있습니다.")

    if response.status_code >= 400:
        print("[경고] MFDS RSS HTTP 오류 응답입니다.")

    return feed


def make_official_item(
    agency: str,
    title: str,
    link: str,
    published_at: datetime | None,
    candidate_order: int,
    rss_url: str = "",
) -> dict:
    return {
        "category": "공식기관 업데이트",
        "title": title,
        "source": agency,
        "link": link,
        "published_at": published_at,
        "official_score": score_official_update(title, agency, rss_url),
        "candidate_order": candidate_order,
    }


def sort_official_candidates(candidates: list[dict]) -> list[dict]:
    return sorted(
        candidates,
        key=lambda item: (
            item["official_score"],
            item["published_at"] or datetime.min.replace(tzinfo=timezone.utc),
            -item["candidate_order"],
        ),
        reverse=True,
    )


def fetch_mfds_fallback_updates(existing_links: set, existing_titles: set) -> list[dict]:
    print("[MFDS fallback] Google News RSS 검색을 시도합니다.")
    candidates = []

    for query in MFDS_FALLBACK_QUERIES:
        rss_url = build_google_news_rss_url(query)
        feed = feedparser.parse(rss_url)
        print(f"  query: {query}")
        print(f"  entries: {len(feed.entries)}")

        for entry in feed.entries:
            title = entry.get("title", "").strip()
            link = entry.get("link", "").strip()
            source = entry.get("source", {}).get("title", "MFDS")
            title = clean_google_news_title(title, source)
            published_at = get_entry_published_datetime(entry)

            if not title or not link:
                continue

            if is_low_quality_official_update(title, "MFDS"):
                continue

            normalized = normalize_title(title)

            if link in existing_links or normalized in existing_titles:
                continue

            candidates.append(
                make_official_item(
                    "MFDS",
                    title,
                    link,
                    published_at,
                    len(candidates),
                )
            )
            existing_links.add(link)
            existing_titles.add(normalized)

    return sort_official_candidates(candidates)[:OFFICIAL_ITEMS_PER_AGENCY]


def fetch_official_updates() -> dict[str, list[dict]]:
    updates_by_agency = {}

    for agency, rss_urls in OFFICIAL_RSS_SOURCES.items():
        print(f"[공식기관 수집] {agency}")
        agency_candidates = []
        seen_links = set()
        seen_titles = set()

        for rss_url in rss_urls:
            try:
                if agency == "MFDS":
                    feed = parse_mfds_rss_with_diagnostics(rss_url)
                else:
                    feed = feedparser.parse(rss_url)
            except Exception as e:
                print(f"[경고] 공식기관 RSS 수집 실패: {agency} / {rss_url} / {e}")
                continue

            if getattr(feed, "bozo", False):
                print(f"[경고] 공식기관 RSS 파싱 경고: {agency} / {rss_url}")

            if agency != "MFDS":
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

                if is_low_quality_official_update(title, agency):
                    continue

                normalized = normalize_title(title)

                if link in seen_links or normalized in seen_titles:
                    continue

                item = make_official_item(
                    agency,
                    title,
                    link,
                    published_at,
                    len(agency_candidates),
                    rss_url,
                )
                agency_candidates.append(item)
                seen_links.add(link)
                seen_titles.add(normalized)

        if agency == "MFDS" and not agency_candidates:
            agency_candidates = fetch_mfds_fallback_updates(seen_links, seen_titles)

        agency_candidates = sort_official_candidates(agency_candidates)
        updates_by_agency[agency] = agency_candidates[:OFFICIAL_ITEMS_PER_AGENCY]
        print(f"  candidates: {len(agency_candidates)}")
        print(f"  selected: {len(updates_by_agency[agency])}")

    return updates_by_agency


def fetch_category_news(
    category_name: str,
    queries: list[str],
    period: str,
    now: datetime,
    lookback_hours: int | None = None,
) -> list[dict]:
    items = []
    seen_links = set()
    seen_titles = set()
    effective_lookback_hours = (
        BRIEFING_LOOKBACK_HOURS[period]
        if lookback_hours is None
        else lookback_hours
    )

    for query in queries:
        rss_url = build_google_news_rss_url(query)
        feed = feedparser.parse(rss_url)

        for entry in feed.entries[:ITEMS_PER_QUERY]:
            if not is_entry_in_lookback_window(entry, now, effective_lookback_hours):
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

        selected = select_final_articles(candidates, FINAL_ITEMS_PER_CATEGORY, category_name)

        if not selected and category_name in CRITICAL_CATEGORY_FALLBACK_QUERIES:
            print(f"[fallback 검색] {category_name}: 최근 {RELAXED_CATEGORY_LOOKBACK_HOURS}시간 기준으로 재검색")
            fallback_candidates = fetch_category_news(
                category_name,
                CRITICAL_CATEGORY_FALLBACK_QUERIES[category_name],
                period,
                now,
                lookback_hours=RELAXED_CATEGORY_LOOKBACK_HOURS,
            )
            selected = select_final_articles(
                fallback_candidates,
                FINAL_ITEMS_PER_CATEGORY,
                f"{category_name} fallback",
            )

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
9. 기사 목록은 별도 HTML 렌더링으로 표시되므로 요약과 실무 시사점 작성에 집중할 것.

기사 목록:
{articles_text}

출력 형식:

🗞️ 핵심 브리핑
문단 작성

──────────

1. 공식기관 업데이트
- 요약:
- 실무 시사점:

2. AI 주요 동향
- 요약:
- 실무 시사점:

3. AI × 제약·바이오
- 요약:
- 실무 시사점:

4. 백신·바이오의약품
- 요약:
- 실무 시사점:

5. GMP·품질·Data Integrity
- 요약:
- 실무 시사점:

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

    try:
        response = requests.post(url, json=payload, timeout=60)
    except Exception:
        print("[Gemini]")
        print(f"model: {GEMINI_MODEL}")
        print("status: failed / request_error")
        raise

    if response.status_code != 200:
        print("[Gemini]")
        print(f"model: {GEMINI_MODEL}")
        print(f"status: failed / {response.status_code}")
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
            lines.append("- 최근 선별 기준을 충족하는 주요 기사가 없습니다.")
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
SECTION_LABEL_STYLE = "font-weight: bold; margin-top: 10px; margin-bottom: 4px;"


def make_article_html(title: str, source: str, link: str, show_source: bool = True) -> str:
    safe_title = escape(title)
    safe_source = escape(source)
    safe_link = escape(link, quote=True)
    title_text = f"{safe_title} · {safe_source}" if show_source else safe_title

    return (
        f'<div style="{ARTICLE_BLOCK_STYLE}">'
        f'<div style="{ARTICLE_TITLE_STYLE}">- {title_text}</div>'
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


def parse_gemini_briefing_sections(briefing: str) -> dict:
    parsed = {
        "core": [],
        "categories": {
            category: {"summary": [], "insight": []}
            for category in CATEGORIES
        },
        "education": [],
    }
    current_section = None
    current_field = None

    for raw_line in briefing.splitlines():
        line = raw_line.strip()

        if not line or set(line) == {"─"}:
            continue

        if "핵심 브리핑" in line:
            current_section = "core"
            current_field = None
            continue

        section_match = re.match(r"^\d+\.\s+(.+)$", line)
        if section_match:
            section_name = section_match.group(1).strip()
            if section_name in CATEGORIES:
                current_section = section_name
                current_field = None
            elif section_name == "교육·홍보자료 활용 포인트":
                current_section = "education"
                current_field = None
            else:
                current_section = None
                current_field = None
            continue

        if line.startswith("- 주요 기사") or line.startswith("- 주요 업데이트"):
            current_field = "skip_articles"
            continue

        if re.match(r"^\d+\)", line) or line.startswith("http"):
            continue

        if current_section == "core":
            parsed["core"].append(line.lstrip("- ").strip())
            continue

        if current_section in CATEGORIES:
            if line.startswith("- 요약:"):
                current_field = "summary"
                value = line.replace("- 요약:", "", 1).strip()
                if value:
                    parsed["categories"][current_section]["summary"].append(value)
                continue

            if line.startswith("- 실무 시사점:"):
                current_field = "insight"
                value = line.replace("- 실무 시사점:", "", 1).strip()
                if value:
                    parsed["categories"][current_section]["insight"].append(value)
                continue

            if current_field in {"summary", "insight"}:
                parsed["categories"][current_section][current_field].append(line.lstrip("- ").strip())
            continue

        if current_section == "education":
            parsed["education"].append(line.lstrip("- ").strip())

    return parsed


def build_fallback_summary(selected_by_category: dict) -> dict:
    parsed = {
        "core": [],
        "categories": {},
        "education": [
            "공식기관 업데이트는 규제·품질 교육자료의 최신 참고 사례로 활용할 수 있습니다.",
            "AI 및 바이오의약품 동향은 교육 도입부와 산업 변화 설명 자료로 활용할 수 있습니다.",
            "GMP·품질 관련 기사는 실무형 사례 토론 주제로 전환할 수 있습니다.",
        ],
    }

    for category in CATEGORIES:
        item_count = len(selected_by_category.get(category, []))
        parsed["categories"][category] = {
            "summary": [f"수집된 주요 기사 {item_count}건을 기준으로 정리했습니다."],
            "insight": ["기사 원문 확인 후 교육·홍보자료 또는 실무 참고자료로 활용할 수 있습니다."],
        }

    return parsed


def make_text_block_html(lines: list[str], empty_text: str) -> str:
    if not lines:
        return f'<div style="margin-bottom: 8px; line-height: 1.6;">{escape(empty_text)}</div>'

    return "\n".join(
        f'<div style="margin-bottom: 8px; line-height: 1.6;">{escape(line)}</div>'
        for line in lines
        if line
    )


def make_general_news_category_html(
    section_number: int,
    category: str,
    items: list[dict],
) -> str:
    blocks = [
        f'<div style="{CATEGORY_TITLE_STYLE}">{section_number}. {escape(category)}</div>',
    ]

    if not items:
        blocks.append('<div style="margin-bottom: 14px;">- 최근 선별 기준을 충족하는 주요 기사가 없습니다.</div>')
        return "\n".join(blocks)

    for item in items:
        blocks.append(
            '<div style="margin-bottom: 14px;">'
            f'<div style="{ARTICLE_TITLE_STYLE}">- {escape(item["title"])}</div>'
            f'<div><a href="{escape(item["link"], quote=True)}">기사 원문 보기</a></div>'
            '</div>'
        )

    return "\n".join(blocks)


def make_education_points_html(points: list[str]) -> str:
    if not points:
        return ""

    blocks = [
        f'<div style="{CATEGORY_TITLE_STYLE}">6. 교육·홍보자료 활용 포인트</div>'
    ]
    for point in points[:3]:
        blocks.append(f'<div style="margin-bottom: 8px; line-height: 1.6;">- {escape(point)}</div>')

    return "\n".join(blocks)


def make_structured_briefing_html(selected_by_category: dict, summary_data: dict) -> str:
    blocks = []

    if summary_data.get("core"):
        blocks.extend(
            [
                '<div style="font-size: 18px; font-weight: bold; margin-top: 4px; margin-bottom: 10px;">핵심 브리핑</div>',
                make_text_block_html(summary_data.get("core", []), "오늘 수집된 주요 이슈를 정리했습니다."),
            ]
        )

    blocks.append(
        make_official_updates_html(
            selected_by_category.get("공식기관 업데이트", {}),
            section_number=1,
        )
    )

    for section_number, category in enumerate(CATEGORIES.keys(), start=2):
        blocks.append(
            make_general_news_category_html(
                section_number,
                category,
                selected_by_category.get(category, []),
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

    if USE_GEMINI:
        try:
            briefing = generate_ai_briefing_with_gemini(selected_by_category, period)
            summary_data = parse_gemini_briefing_sections(briefing)
        except Exception as e:
            print("[경고] Gemini 브리핑 생성 실패. 기본 브리핑으로 대체합니다.")
            print(e)
            summary_data = build_fallback_summary(selected_by_category)
    else:
        summary_data = build_fallback_summary(selected_by_category)

    briefing_html = make_structured_briefing_html(selected_by_category, summary_data)

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
    utc_now = now.astimezone(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    period = get_briefing_period(now)
    briefing_label = get_briefing_label(period)

    print("========================================")
    print("Daily AI·GMP·Bio Briefing Bot")
    print("========================================")
    # GitHub Actions 실행 로그에서 스케줄/타임존을 확인하기 위한 출력입니다.
    print("[실행 시간]")
    print(f"UTC: {utc_now.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"KST: {now.strftime('%Y-%m-%d %H:%M:%S')}")
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
