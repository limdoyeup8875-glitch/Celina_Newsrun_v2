# -*- coding: utf-8 -*-
"""
커버리지 뉴스런 -> 텔레그램 자동 발송 봇
- 네이버 뉴스 검색 API 사용
- 요약: Groq(무료 티어, OpenAI 호환 API) 사용
- 섹터별로 묶어서 (제목 / 링크 / 2~3불릿 요약) 전송
- KST 기준 평일에만 동작, 월요일은 72시간 / 그 외 평일은 24시간 조회
- 이미 보낸 기사는 seen.json 으로 중복 제거
- 발송 시각(평일 06시·16시 KST)은 워크플로 cron 이 제어. 수동 실행은 언제든 가능.
"""

import os
import re
import sys
import json
import time
import html
import datetime
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo

import requests

# 로그가 실시간으로 보이도록 표준출력 라인버퍼링
# (GitHub Actions 등 비-터미널 환경에서 진행상황이 바로 찍히게 함)
try:
    sys.stdout.reconfigure(line_buffering=True)
except Exception:
    pass

# =====================================================================
# 1) 설정 (여기만 고치면 됩니다)
# =====================================================================

# 섹터별 종목.  "표시이름": [네이버에서 검색할 키워드들]
# 키워드를 여러 개 넣으면 그만큼 더 폭넓게 잡습니다(중복은 자동 제거).
SECTORS = {
    "Auto": {
        "현대차":      ["현대차", "현대자동차"],
        "현대모비스":  ["현대모비스"],
        "만도":        ["HL만도", "만도"],
        "한국타이어":  ["한국타이어"],
        "한온시스템":  ["한온시스템"],
    },
    # ── Global auto peers (해외 완성차) ────────────────────────────────
    # 국내 기사 기준 표기법이 여러 개인 곳은 별칭을 함께 넣었습니다.
    #   · 토요타: '토요타' / '도요타' 두 표기 혼용 → 둘 다
    #   · BYD: 'BYD' / '비야디' 혼용 → 둘 다
    #
    # ▶ GM, 지리(Geely)는 단어가 짧거나(GM) 다른 뜻과 겹쳐서(지리산·지리적)
    #   단순 제목필터만으로는 노이즈가 큽니다. 그래서 dict 형태로 적어
    #   문맥필터(ctx)를 겁니다:
    #     - "q"  : 검색어 겸 제목필터 키워드 (제목에 이 중 하나가 있어야 채택)
    #     - "ctx": 제목+요약 스니펫에 이 중 하나가 있어야 최종 채택
    #   → GM: 'GM 작물(GMO)'·'게임 GM' 등은 자동차 문맥어가 없어 탈락
    #   → 지리: '지리산' 등은 자동차 문맥어가 없어 탈락
    #   (일반 종목은 기존처럼 리스트만 적으면 되고 문맥필터는 생략됩니다.)
    "Global Auto": {
        "폭스바겐":    ["폭스바겐"],
        "BMW":         ["BMW"],
        "GM": {
            "q":   ["제너럴모터스", "GM"],
            "ctx": ["제너럴모터스", "자동차", "완성차", "전기차",
                    "쉐보레", "캐딜락", "GMC", "픽업", "메리 바라"],
        },
        "스텔란티스":  ["스텔란티스", "Stellantis"],
        "BYD":         ["BYD", "비야디"],
        "지리(Geely)": {
            "q":   ["지리", "지리자동차", "Geely"],
            "ctx": ["자동차", "전기차", "완성차"],
        },
        "토요타":      ["토요타", "도요타"],
        "혼다":        ["혼다"],
        "스바루":      ["스바루"],
        "스즈키":      ["스즈키"],
    },
    "EV": {
        "LG에너지솔루션": ["LG에너지솔루션", "LG엔솔"],
        "삼성SDI":        ["삼성SDI"],
        "SK이노베이션":   ["SK이노베이션"],
        "포스코퓨처엠":   ["포스코퓨처엠"],
        "엘앤에프":       ["엘앤에프"],
    },
    "Construction": {
        "현대건설":   ["현대건설"],
        "GS건설":     ["GS건설"],
        "삼성E&A":    ["삼성E&A", "삼성엔지니어링"],
        "삼성C&T":    ["삼성물산"],
    },
    "Shipbuilding": {
        "한화오션":        ["한화오션"],
        "삼성중공업":      ["삼성중공업"],
        "현대중공업":      ["HD현대중공업", "현대중공업"],
        "한화엔진":        ["한화엔진"],
        "HD한국조선해양":  ["HD한국조선해양", "한국조선해양"],
    },
    "Metal": {
        "고려아연":     ["고려아연"],
        "포스코홀딩스": ["포스코홀딩스", "POSCO홀딩스"],
    },
}

# 한 종목당 한 번에 보낼 최대 기사 수 (메시지가 너무 길어지는 것 방지)
MAX_PER_COMPANY = 8

# 요약 방식
#   True  : Groq(무료 티어)로 2~3불릿 요약 -> 품질 좋음
#   False : 네이버가 주는 기사 요약문(스니펫)을 그대로 정리 -> 추가설정 없음
USE_LLM = True

# 기사 본문까지 직접 읽어서 요약 (요약 품질↑, 약간 느려짐). 실패하면 자동으로 미리보기로 대체
FETCH_FULL_TEXT = True

# 주말(토/일)에는 발송하지 않음.
#   ※ 이 설정은 '예약(cron) 실행'에만 적용됩니다. 화면에서 손으로 돌리는
#     수동 실행(Run workflow)은 테스트 목적이라 주말/시간과 무관하게 항상 실행됩니다.
SKIP_WEEKEND = True

# =====================================================================
# 2) 비밀값 (GitHub Secrets 에서 자동으로 읽어옵니다 - 코드에 직접 적지 마세요)
# =====================================================================
NAVER_ID     = os.environ.get("NAVER_CLIENT_ID", "")
NAVER_SECRET = os.environ.get("NAVER_CLIENT_SECRET", "")
TG_TOKEN     = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_CHAT      = os.environ.get("TELEGRAM_CHAT_ID", "")
GROQ_KEY = os.environ.get("GROQ_API_KEY", "")

# 예약 실행 여부 (워크플로에서 GITHUB_EVENT_NAME 을 넘겨줌).
#   "schedule" = cron 자동 실행 / "workflow_dispatch" = 수동 실행
IS_SCHEDULED = os.environ.get("GITHUB_EVENT_NAME", "") == "schedule"

KST = ZoneInfo("Asia/Seoul")
SEEN_FILE = "seen.json"
SEEN_KEEP_DAYS = 14  # 중복 기록을 며칠 보관할지

# =====================================================================
# 3) 유틸 함수
# =====================================================================

def now_kst():
    return datetime.datetime.now(tz=KST)


def lookback_hours():
    """월요일이면 72시간, 그 외 평일이면 24시간"""
    return 72 if now_kst().weekday() == 0 else 24


def is_weekend():
    return now_kst().weekday() >= 5  # 5=토, 6=일


def strip_tags(text):
    """네이버가 돌려주는 <b> 태그, HTML 엔티티 정리"""
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text).strip()


def company_config(value):
    """종목 설정을 (검색어 리스트, 문맥어 리스트)로 표준화.
    - 리스트로 적으면: 그대로 검색어=제목필터, 문맥필터 없음(None)
    - dict로 적으면:  q=검색어 겸 제목필터, ctx=제목+요약에 있어야 할 문맥어
    """
    if isinstance(value, dict):
        return value.get("q", []), value.get("ctx") or None
    return value, None


def naver_news(query, display):
    """네이버 뉴스 검색. 최신순(sort=date). 차단 방지를 위해 간격/재시도 포함."""
    url = "https://openapi.naver.com/v1/search/news.json"
    headers = {
        "X-Naver-Client-Id": NAVER_ID,
        "X-Naver-Client-Secret": NAVER_SECRET,
    }
    params = {"query": query, "display": display, "sort": "date"}
    for attempt in range(3):
        try:
            r = requests.get(url, headers=headers, params=params, timeout=15)
            r.raise_for_status()
            time.sleep(0.4)   # 연속 호출 사이 간격 (네이버 차단 방지)
            return r.json().get("items", [])
        except Exception:
            if attempt < 2:
                time.sleep(1.5)
                continue
            raise


def parse_pubdate(s):
    """'Mon, 26 Sep 2022 09:00:00 +0900' -> KST aware datetime"""
    dt = parsedate_to_datetime(s)
    return dt.astimezone(KST)


GROQ_MODEL = "llama-3.3-70b-versatile"   # 문체 지시(개조식/명사형)를 잘 따르는 대형 모델
# 속도·한도가 더 필요하면 아래로(단 문체 준수는 약해짐):
#   GROQ_MODEL = "llama-3.1-8b-instant"

# 분당 한도(429) 방지를 위해 호출 사이 최소 간격을 둠
LLM_MIN_INTERVAL = 15.0   # 초 (70B는 분당 토큰 한도가 낮아 넉넉히. 8B로 되돌리면 8.0 권장)
_last_llm = [0.0]

# 429가 계속 나면(=계정이 실제로 스로틀됨) 이번 실행은 요약을 포기하고
# 남은 기사를 전부 스니펫으로 처리 → 실행이 무한정 늘어지는 것 방지.
_llm_consecutive_429 = [0]     # 연속 429 횟수
_llm_disabled = [False]        # True 가 되면 이후 LLM 호출 안 함
LLM_429_GIVEUP = 8             # 연속 429 이 만큼 쌓이면 요약 중단

def _pace_llm():
    wait = LLM_MIN_INTERVAL - (time.monotonic() - _last_llm[0])
    if wait > 0:
        time.sleep(wait)
    _last_llm[0] = time.monotonic()


def summarize_with_llm(title, body):
    """Groq(OpenAI 호환 API)로 2불릿 개조식 요약 (추가 패키지 없이 REST 호출)"""
    url = "https://api.groq.com/openai/v1/chat/completions"
    prompt = (
        "다음 뉴스 기사를 한국어로 핵심만 요약해줘.\n"
        "반드시 아래 규칙을 지켜:\n"
        "1) 불릿 정확히 2개만. 각 불릿은 새 줄에서 '- ' 로 시작.\n"
        "2) 【매우 중요】 각 불릿은 반드시 '명사형(개조식)'으로 끝낼 것. "
        "즉 마지막을 명사 또는 명사형으로 맺어라 "
        "(예: '...확대', '...전망', '...추진', '...예정', '...기록', '...밝힘', '...계획').\n"
        "3) '~했다 / ~한다 / ~이다 / ~된다 / ~있다 / ~였다' 같은 서술형 문장으로 끝내면 절대 안 됨. "
        "서술형이 떠오르면 명사형으로 바꿔라 "
        "(예: '실적이 개선됐다' → '실적 개선', '투자를 확대한다' → '투자 확대', "
        "'흑자 전환했다' → '흑자 전환').\n"
        "4) 서론·맺음말 없이 사실만, 숫자는 살려서, 각 불릿은 한 줄로 짧게.\n\n"
        f"[제목]\n{title}\n\n[본문]\n{body[:2500]}"
    )
    payload = {
        "model": GROQ_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.3,
        "max_tokens": 400,
    }

    # 이미 이번 실행에서 429가 과도해 요약을 접었으면 즉시 스니펫으로
    if _llm_disabled[0]:
        raise RuntimeError("LLM 비활성화(429 과다) — 스니펫 사용")

    r = None
    for attempt in range(3):
        _pace_llm()
        r = requests.post(
            url,
            headers={"Authorization": f"Bearer {GROQ_KEY}",
                     "Content-Type": "application/json"},
            json=payload,
            timeout=30,
        )
        if r.status_code == 429:
            # 분당 요청/토큰 한도. Groq은 분 단위로 리셋되므로 잠깐 기다렸다 재시도.
            _llm_consecutive_429[0] += 1
            if _llm_consecutive_429[0] >= LLM_429_GIVEUP:
                _llm_disabled[0] = True
                print("  429가 계속 발생 → 이번 실행은 남은 기사 모두 스니펫으로 처리")
                r.raise_for_status()
            try:                                    # Groq이 알려주는 대기시간(retry-after) 우선
                wait = int(float(r.headers.get("retry-after", "30")))
            except Exception:
                wait = 30
            wait = min(max(wait, 5), 60)            # 5~60초 범위로 clamp
            print(f"  429(한도) → {wait}초 대기 후 재시도 ({attempt + 1}/3)")
            time.sleep(wait)
            continue
        if r.status_code == 503:
            # 서버 일시 과부하 → 짧게 쉬고 재시도
            time.sleep(10 * (attempt + 1))       # 10s, 20s, 30s
            continue
        r.raise_for_status()
        data = r.json()
        _llm_consecutive_429[0] = 0              # 성공하면 연속 429 카운터 리셋
        return data["choices"][0]["message"]["content"].strip()
    r.raise_for_status()   # 재시도 모두 실패 -> 예외 발생 -> 스니펫으로 대체


def _clean_title(t):
    """네이버 페이지 제목 뒤에 붙는 ' : 네이버 뉴스' 류 꼬리표 제거,
    그리고 제목이 잘려서 붙는 '맨 끝의 …/...' 만 제거(문장 중간의 … 는 원문 스타일이라 보존)."""
    t = strip_tags(t)
    t = re.sub(r"\s*[:\-|]\s*네이버\s*뉴스\s*$", "", t)
    # 맨 끝에 붙은 생략부호(잘림 표시)만 제거. 중간의 …(예: '재평가…"올해')는 그대로 둠
    t = re.sub(r"\s*(\.\.\.+|…)\s*$", "", t)
    return t.strip()


def _meta_content(h, key):
    """<meta property/name="key" content="..."> 를 속성 순서와 무관하게 추출.
    (네이버 페이지마다 property/content 순서가 달라 og:title 추출이 실패하던 문제 대응)"""
    # 패턴 A: property/name 이 앞
    m = re.search(
        rf'<meta[^>]+(?:property|name)=["\']{re.escape(key)}["\'][^>]+content=["\'](.*?)["\']',
        h, flags=re.S | re.I,
    )
    if m:
        return m.group(1)
    # 패턴 B: content 가 앞
    m = re.search(
        rf'<meta[^>]+content=["\'](.*?)["\'][^>]+(?:property|name)=["\']{re.escape(key)}["\']',
        h, flags=re.S | re.I,
    )
    if m:
        return m.group(1)
    return ""


def _extract_full_title(h):
    """네이버 기사 HTML 에서 '풀 제목'을 최대한 정확히 뽑아냄.
    여러 후보(본문 헤드라인 h2 / og:title / twitter:title / <title>)를 모아
    가장 길고 온전한 것을 선택 → API가 '…'로 잘라 준 제목을 원문 풀 제목으로 대체."""
    candidates = []

    # 1) 본문 헤드라인 요소 (가장 신뢰도 높은 '렌더링된' 풀 제목)
    for pat in (
        r'<h2[^>]*media_end_head_headline[^>]*>(.*?)</h2>',   # 현재 뉴스 뷰(PC/모바일)
        r'id=["\']title_area["\'][^>]*>(.*?)</h2>',           # 일부 레이아웃
        r'id=["\']articleTitle["\'][^>]*>(.*?)</h[0-9]>',     # 구버전
        r'class=["\'][^"\']*end_tit[^"\']*["\'][^>]*>(.*?)</h[0-9]>',  # 구버전
    ):
        mh = re.search(pat, h, flags=re.S | re.I)
        if mh:
            candidates.append(mh.group(1))

    # 2) 메타 태그 (속성 순서 무관 추출)
    candidates.append(_meta_content(h, "og:title"))
    candidates.append(_meta_content(h, "twitter:title"))

    # 3) <title> (맨 마지막 대체용)
    mt = re.search(r'<title[^>]*>(.*?)</title>', h, flags=re.S | re.I)
    if mt:
        candidates.append(mt.group(1))

    cleaned = [_clean_title(c) for c in candidates if c and c.strip()]
    if not cleaned:
        return ""
    # 잘린 제목보다 온전한 제목이 대체로 더 길다 → 가장 긴 후보 선택
    return max(cleaned, key=len)


def fetch_article(url):
    """네이버 기사에서 (전체 제목, 본문) 추출. 한 번의 요청으로 둘 다 가져옴.
    실패하는 항목은 빈 문자열로 반환(자동 대체)."""
    try:
        ua = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
        r = requests.get(url, headers={"User-Agent": ua}, timeout=10)
        r.raise_for_status()
        h = r.text
        # (1) 전체 제목: 여러 후보(헤드라인 h2 / og:title / twitter:title / <title>)
        #     중 가장 온전한 것을 선택 → API가 '…'로 잘라 준 제목을 원문 풀 제목으로 대체
        title = _extract_full_title(h)
        # (2) 본문
        h2 = re.sub(r"<script.*?</script>", " ", h, flags=re.S | re.I)
        h2 = re.sub(r"<style.*?</style>", " ", h2, flags=re.S | re.I)
        m = re.search(r'id="dic_area"[^>]*>(.*?)</article>', h2, flags=re.S | re.I)
        if not m:
            m = re.search(r'id="newsct_article"[^>]*>(.*?)</div>', h2, flags=re.S | re.I)
        body = strip_tags(m.group(1)) if m else ""
        body = re.sub(r"\s+", " ", body).strip()
        return title, body
    except Exception as e:
        print(f"  기사 추출 실패: {e}")
        return "", ""


def _no_ellipsis(s):
    """문장 끝/중간의 ... … 같은 생략부호 제거"""
    s = re.sub(r"\s*(\.\.\.+|…)\s*", " ", s)
    return s.strip()


def clean_summary(text):
    """LLM 요약 정리: 줄바꿈·인라인마커·번호목록을 모두 개별 불릿으로 분리,
    기호는 '-'로 통일, 서론/생략부호 제거."""
    text = text.strip()
    raw = []
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        # 한 줄에 ' - ' 등으로 이어붙은 경우 분리
        raw.extend(re.split(r"\s[\-\*\u2022\u00b7]\s+", line))
    out = []
    for p in raw:
        s = p.strip()
        if not s:
            continue
        s = re.sub(r"^[\-\*\u2022\u00b7]\s*", "", s)        # 맨 앞 불릿기호 제거
        s = re.sub(r"^\d{1,2}[\.\)]\s+", "", s).strip()     # '1.' '2)' 번호마커 제거
        if not s:
            continue
        if s.endswith(":") or s.endswith("："):              # 서론(콜론으로 끝) 제거
            continue
        if re.match(r"^(다음은|아래는|아래의|이 기사|기사의|뉴스의)", s):
            continue
        s = _no_ellipsis(s)
        if s:
            out.append(f"- {s}")
    return "\n".join(out[:2]) if out else "- (요약 없음)"


def summarize_snippet(snippet):
    """무료 모드/대체: 네이버 스니펫을 불릿으로 정리"""
    clean = _no_ellipsis(strip_tags(snippet))
    # 문장 단위로 잘라 최대 3개 불릿
    parts = re.split(r"(?<=[.!?。])\s+", clean)
    parts = [_no_ellipsis(p) for p in parts if len(p.strip()) > 5][:2]
    if not parts:
        parts = [clean] if clean else ["(요약 없음)"]
    return "\n".join(f"- {p}" for p in parts)


def make_summary(item, body=None):
    title = strip_tags(item["title"])
    snippet = strip_tags(item.get("description", ""))
    if USE_LLM:
        text = snippet
        if body:                       # build_article_block에서 이미 받아둔 본문 재사용
            if len(body) > len(snippet):
                text = body
        elif FETCH_FULL_TEXT:
            link = item.get("link") or item.get("originallink") or ""
            if "naver.com" in link:    # 네이버 호스팅 기사만 본문 추출
                _t, full = fetch_article(link)
                if len(full) > len(snippet):
                    text = full
        try:
            raw = summarize_with_llm(title, text)
            return clean_summary(raw)
        except Exception as e:
            print(f"  요약 실패(스니펫으로 대체): {e}")
            return summarize_snippet(item.get("description", ""))
    return summarize_snippet(item.get("description", ""))


def tg_send(text):
    """텔레그램으로 한 메시지 전송 (HTML 모드)"""
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    data = {
        "chat_id": TG_CHAT,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    r = requests.post(url, data=data, timeout=15)
    if r.status_code != 200:
        print("텔레그램 오류:", r.text)
    time.sleep(0.5)  # 메시지 속도 제한 회피


def tg_send_long(text):
    """4096자 제한을 넘으면 줄 단위로 쪼개서 전송"""
    LIMIT = 3800
    if len(text) <= LIMIT:
        tg_send(text)
        return
    buf = ""
    for line in text.split("\n"):
        if len(buf) + len(line) + 1 > LIMIT:
            tg_send(buf)
            buf = ""
        buf += line + "\n"
    if buf.strip():
        tg_send(buf)


# ---- 중복 제거용 저장 파일 ----

def load_seen():
    try:
        with open(SEEN_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_seen(seen):
    # 오래된 기록 정리
    cutoff = (now_kst() - datetime.timedelta(days=SEEN_KEEP_DAYS)).isoformat()
    seen = {k: v for k, v in seen.items() if v >= cutoff}
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(seen, f, ensure_ascii=False, indent=0)


# =====================================================================
# 4) 메인
# =====================================================================

def build_article_block(item):
    link = item.get("link") or item.get("originallink")
    api_title = strip_tags(item["title"])

    # 네이버 호스팅 기사면 원문에서 전체 제목 + 본문을 한 번에 가져옴
    # (API가 잘라 준 '...' 제목을 원문 풀 제목으로 대체)
    full_title, body = "", None
    if link and "naver.com" in link and FETCH_FULL_TEXT:
        full_title, body = fetch_article(link)

    title = full_title or api_title
    summary = make_summary(item, body=body)   # 위에서 받은 본문 재사용(중복 요청 방지)

    # HTML 이스케이프 (제목/요약/링크 안의 < > & 처리)
    safe_title = html.escape(title)
    safe_summary = html.escape(summary)
    safe_link = html.escape(link) if link else ""
    # 형식: ■ <b>제목</b>   /   링크(별도 줄, 하이퍼링크 아님)   /   요약
    parts = [f"■ <b>{safe_title}</b>"]
    if safe_link:
        parts.append(safe_link)   # 텔레그램이 URL을 자동으로 클릭 가능하게 처리
    parts.append(safe_summary)
    return "\n".join(parts)


def main():
    # 비밀값 체크
    missing = [n for n, v in [
        ("NAVER_CLIENT_ID", NAVER_ID), ("NAVER_CLIENT_SECRET", NAVER_SECRET),
        ("TELEGRAM_BOT_TOKEN", TG_TOKEN), ("TELEGRAM_CHAT_ID", TG_CHAT),
    ] if not v]
    if USE_LLM and not GROQ_KEY:
        missing.append("GROQ_API_KEY")
    if missing:
        print("환경변수(Secrets)가 비어있습니다:", ", ".join(missing))
        sys.exit(1)

    # 주말 스킵: 예약(cron) 실행일 때만 적용. 수동 실행은 테스트용이라 항상 진행.
    if IS_SCHEDULED and SKIP_WEEKEND and is_weekend():
        print("주말이므로 실행하지 않습니다. (예약 실행)")
        return

    hours = lookback_hours()
    cutoff = now_kst() - datetime.timedelta(hours=hours)
    seen = load_seen()
    now_iso = now_kst().isoformat()

    header = (
        f"🗞️ <b>뉴스런</b>  {now_kst().strftime('%Y-%m-%d %H:%M')} KST\n"
        f"(최근 {hours}시간 · 신규 기사만)"
    )
    sector_messages = []
    total_new = 0

    for sector, companies in SECTORS.items():
        lines = [f"\n<b>━━ {sector} ━━</b>"]
        sector_has = False

        for disp_name, cfg in companies.items():
            keywords, ctx_words = company_config(cfg)
            collected = []
            local_seen_links = set()
            for kw in keywords:
                try:
                    items = naver_news(kw, display=(100 if hours >= 72 else 30))
                except Exception as e:
                    print(f"  검색 실패 [{kw}]: {e}")
                    continue
                for it in items:
                    link = it.get("link") or it.get("originallink")
                    if not link:
                        continue
                    # 제목 필터: 제목에 검색 키워드가 들어간 기사만 채택
                    title_plain = strip_tags(it.get("title", ""))
                    if not any(k in title_plain for k in keywords):
                        continue
                    # 문맥 필터: ctx_words가 지정된 종목(GM·지리)은
                    # 제목+요약 스니펫에 문맥어가 하나라도 있어야 채택
                    if ctx_words:
                        snippet_plain = strip_tags(it.get("description", ""))
                        haystack = f"{title_plain} {snippet_plain}"
                        if not any(c in haystack for c in ctx_words):
                            continue
                    # 시간창 필터
                    try:
                        if parse_pubdate(it["pubDate"]) < cutoff:
                            continue
                    except Exception:
                        continue
                    # 중복 필터 (이전 실행 + 이번 실행 내)
                    if link in seen or link in local_seen_links:
                        continue
                    local_seen_links.add(link)
                    collected.append(it)

            # 최신순 정렬 후 상한 적용
            collected.sort(key=lambda x: x.get("pubDate", ""), reverse=True)
            collected = collected[:MAX_PER_COMPANY]
            if not collected:
                continue

            sector_has = True
            print(f"  [{sector}] {disp_name}: {len(collected)}건 요약 중...")
            lines.append(f"\n<u>{html.escape(disp_name)}</u>")
            for it in collected:
                lines.append(build_article_block(it))
                link = it.get("link") or it.get("originallink")
                seen[link] = now_iso
                total_new += 1

        if sector_has:
            sector_messages.append("\n".join(lines))
        print(f"[{sector}] 처리 완료 (누적 신규 {total_new}건)")

    # 전송
    tg_send(header)
    if total_new == 0:
        tg_send("✅ 새로운 기사가 없습니다.")
    else:
        for msg in sector_messages:
            tg_send_long(msg)

    save_seen(seen)
    print(f"완료: 신규 {total_new}건 전송")


if __name__ == "__main__":
    main()
