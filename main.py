
import os
import re
import hmac
import hashlib
import base64
import logging
import time
import uuid
import unicodedata
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
from threading import Lock

import requests
import psycopg
from psycopg.rows import dict_row
from flask import Flask, request, abort, jsonify

app = Flask(__name__)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("thecolor-line-bot")

# =========================================================
# 환경변수
# =========================================================

LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET", "")
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
DATABASE_URL = os.getenv("DATABASE_URL", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY", "")

KST = ZoneInfo("Asia/Seoul")

# 관리자 키워드: 실제 표시 이름 전체가 아니라 포함 여부로 검색
ADMIN_NAMES = [
    "무무",
    "이리",
    "타워",
    "렌치",
    "주음",
    "꼬꼬",
    "짜필",
]

MAX_HISTORY_TURNS = 10
PROFILE_CACHE_SECONDS = 21600
PROFILE_FAILURE_CACHE_SECONDS = 60
GEMINI_REQUEST_TIMEOUT = 9
MODEL_COOLDOWN_SECONDS = 90

conversation_histories = {}
history_lock = Lock()

profile_cache = {}
profile_lock = Lock()

model_cooldowns = {}
cooldown_lock = Lock()

_db_initialized = False
db_init_lock = Lock()

# Gemini 모델은 환경변수 GEMINI_MODEL로 우선 지정 가능
DEFAULT_GEMINI_MODELS = [
    "gemini-3.6-flash",
    "gemini-3.1-flash-lite",
    "gemini-3.5-flash-lite",
    "gemini-3.7-flash",
    "gemini-3.8-flash",
]

SYSTEM_PROMPT = """
너는 더컬러 단체 채팅방의 AI 캐릭터 '레드봇 2.0'이다.

- 자기 이름은 반드시 '레드봇 2.0'이라고 한다.
- 자신을 '집사', 'AI 집사', '집사님'이라고 부르거나 소개하지 않는다.
- 사용자가 누구냐고 물으면 '저는 레드봇 2.0이에요.'라고 답한다.
- 기본적으로 자연스럽고 친근한 한국어 존댓말을 사용한다.
- 일반 질문에는 핵심부터 답하고 불필요하게 길게 말하지 않는다.
- 질문에 필요한 설명은 빠뜨리지 않고 완결된 문장으로 답한다.
- 사용자가 고민을 이야기하면 먼저 공감하고 상황에 맞게 조언한다.
- 확인하지 못한 사실이나 방 규칙을 지어내지 않는다.
- 확실하지 않은 정보는 확실하지 않다고 말한다.
- 검색 결과가 있으면 검색 결과를 바탕으로 답하되, 결과에 없는 내용을 사실처럼 만들지 않는다.
- 이전 대화가 제공되면 문맥을 이어서 답한다.
- 답변은 문장 중간에서 끊지 않는다.
- 일반 답변은 1~3문장 정도로 작성하되, 자세한 설명을 요청하면 충분히 설명한다.
"""

SEARCH_KEYWORDS = [
    "오늘", "어제", "내일", "최신", "최근", "뉴스",
    "날씨", "환율", "주가", "검색", "현재", "지금",
    "이번 주", "이번달", "이번 달", "가격", "출시",
    "업데이트", "사건", "이슈", "몇 시", "영업시간",
    "공식", "근황", "순위", "실시간",
]


# =========================================================
# 데이터베이스
# =========================================================

def get_db():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL 환경변수가 설정되지 않았습니다.")

    return psycopg.connect(
        DATABASE_URL,
        row_factory=dict_row,
        connect_timeout=10,
    )


def init_database():
    global _db_initialized

    if _db_initialized:
        return

    with db_init_lock:
        if _db_initialized:
            return

        with get_db() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS message_events (
                    event_id TEXT PRIMARY KEY,
                    group_id TEXT,
                    user_id TEXT,
                    display_name TEXT,
                    message_type TEXT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)

            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_message_events_created
                ON message_events (created_at)
            """)

            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_message_events_group_user
                ON message_events (group_id, user_id)
            """)

            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_message_events_name
                ON message_events (display_name)
            """)

        _db_initialized = True
        logger.info("데이터베이스 초기화 완료")


def ensure_database():
    try:
        init_database()
    except Exception:
        logger.exception("데이터베이스 초기화 실패")
        raise


# =========================================================
# LINE API
# =========================================================

def verify_signature(body, signature):
    if not LINE_CHANNEL_SECRET or not signature:
        return False

    digest = hmac.new(
        LINE_CHANNEL_SECRET.encode("utf-8"),
        body,
        hashlib.sha256,
    ).digest()

    expected = base64.b64encode(digest).decode("utf-8")
    return hmac.compare_digest(expected, signature)


def line_api(endpoint, payload):
    if not LINE_CHANNEL_ACCESS_TOKEN:
        raise RuntimeError(
            "LINE_CHANNEL_ACCESS_TOKEN 환경변수가 설정되지 않았습니다."
        )

    url = f"https://api.line.me/v2/bot/{endpoint}"

    response = requests.post(
        url,
        headers={
            "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=8,
    )

    if not response.ok:
        logger.error(
            "LINE API 오류 endpoint=%s status=%s body=%s",
            endpoint,
            response.status_code,
            response.text[:1500],
        )

    response.raise_for_status()
    return response


def reply_text(reply_token, text):
    if not reply_token:
        return

    text = str(text or "").strip()
    if not text:
        text = "답변을 만들지 못했어요. 다시 시도해 주세요."

    text = text[:4900]

    try:
        line_api("message/reply", {
            "replyToken": reply_token,
            "messages": [{
                "type": "text",
                "text": text,
            }],
        })
        logger.info("LINE 답장 전송 성공")
    except Exception:
        logger.exception("LINE 답장 전송 실패")


def reply_text_v2(reply_token, text, substitutions):
    """LINE textV2를 사용해 실제 사용자 멘션을 보냅니다."""
    if not reply_token:
        return

    # LINE textV2의 substitution은 리스트가 아닌 placeholder 키를 가진 객체입니다.
    substitution_map = {}
    for item in (substitutions or [])[:20]:
        placeholder = str(item.get("placeholder") or "").strip()
        key = placeholder.strip("{}")
        mentionee = item.get("mentionee") or {}
        user_id = str(mentionee.get("userId") or "").strip()
        if not key or not user_id:
            continue
        substitution_map[key] = {
            "type": "mention",
            "mentionee": {
                "type": "user",
                "userId": user_id,
            },
        }

    message = {
        "type": "textV2",
        "text": str(text or "")[:4900],
    }
    if substitution_map:
        message["substitution"] = substitution_map

    payload = {
        "replyToken": reply_token,
        "messages": [message],
    }
    line_api("message/reply", payload)


# =========================================================
# 사용자 프로필
# =========================================================

def get_member_profile(group_id, user_id):
    if not group_id or not user_id:
        return None

    try:
        response = requests.get(
            (
                "https://api.line.me/v2/bot/group/"
                f"{group_id}/member/{user_id}"
            ),
            headers={
                "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
            },
            timeout=4,
        )

        if not response.ok:
            logger.warning(
                "프로필 조회 실패 group=%s user=%s status=%s body=%s",
                group_id,
                user_id,
                response.status_code,
                response.text[:500],
            )
            return None

        data = response.json()
        return {
            "userId": user_id,
            "displayName": data.get("displayName", ""),
            "pictureUrl": data.get("pictureUrl", ""),
        }

    except Exception:
        logger.exception("사용자 프로필 조회 오류")
        return None


def get_cached_profile(group_id, user_id):
    key = (group_id, user_id)
    now = time.time()

    with profile_lock:
        cached = profile_cache.get(key)
        if cached:
            ttl = PROFILE_CACHE_SECONDS if cached["profile"] else PROFILE_FAILURE_CACHE_SECONDS
            if now - cached["time"] < ttl:
                return cached["profile"]

    profile = get_member_profile(group_id, user_id)
    with profile_lock:
        profile_cache[key] = {"time": now, "profile": profile}

    # 이름을 이번에 알아냈으면 과거 기록의 이름도 보완합니다.
    if profile and profile.get("displayName"):
        try:
            with get_db() as conn:
                conn.execute("""
                    UPDATE message_events
                    SET display_name = %s
                    WHERE group_id = %s AND user_id = %s
                      AND (display_name IS NULL OR display_name = '' OR display_name = user_id)
                """, (profile["displayName"], group_id, user_id))
        except Exception:
            logger.exception("과거 메시지 표시 이름 보완 실패")

    return profile


# =========================================================
# 메시지 저장
# =========================================================

def record_message(event):
    source = event.get("source") or {}
    message = event.get("message") or {}

    group_id = source.get("groupId") or source.get("roomId")
    user_id = source.get("userId")

    if not user_id:
        return

    display_name = ""
    if group_id:
        profile = get_cached_profile(group_id, user_id)
        if profile:
            display_name = profile.get("displayName", "")

    # 프로필 API가 실패하면 기존 DB에 저장된 표시 이름을 유지합니다.
    # user_id를 display_name에 저장하면 순위에 긴 ID가 노출되므로 저장하지 않습니다.
    if not display_name and group_id:
        try:
            with get_db() as conn:
                previous = conn.execute("""
                    SELECT display_name
                    FROM message_events
                    WHERE group_id = %s
                      AND user_id = %s
                      AND display_name IS NOT NULL
                      AND display_name <> ''
                      AND display_name <> user_id
                    ORDER BY created_at DESC
                    LIMIT 1
                """, (group_id, user_id)).fetchone()
            if previous:
                display_name = previous["display_name"] or ""
        except Exception:
            logger.exception("기존 표시 이름 조회 실패")

    event_id = (
        event.get("webhookEventId")
        or message.get("id")
        or str(uuid.uuid4())
    )

    message_type = message.get("type", "unknown")

    with get_db() as conn:
        conn.execute("""
            INSERT INTO message_events (
                event_id,
                group_id,
                user_id,
                display_name,
                message_type,
                created_at
            )
            VALUES (
                %s, %s, %s, %s, %s,
                COALESCE(%s::timestamptz, NOW())
            )
            ON CONFLICT (event_id) DO NOTHING
        """, (
            event_id,
            group_id,
            user_id,
            display_name,
            message_type,
            event.get("timestamp") and datetime.fromtimestamp(
                event["timestamp"] / 1000,
                tz=ZoneInfo("UTC"),
            ).isoformat(),
        ))


# =========================================================
# 관리자 검색 및 실제 멘션
# =========================================================

def normalize_name(value):
    """
    이름 검색용 정규화.
    유니코드 호환 문자를 정리하고 공백을 제거합니다.
    이모티콘이나 장식 기호가 남아 있어도 부분 검색은 가능합니다.
    """
    if not value:
        return ""

    value = unicodedata.normalize("NFKC", str(value))
    value = value.casefold()
    value = re.sub(r"\s+", "", value)

    return value


def find_admin_users(group_id):
    """
    현재 그룹에서 관찰된 사용자 ID와 실제 표시 이름을 조회합니다.
    키워드와 표시 이름은 정확히 일치할 필요 없이 부분 일치합니다.
    """

    if not group_id:
        return {}, ["현재 그룹 ID를 찾을 수 없습니다."]

    with get_db() as conn:
        rows = conn.execute("""
            SELECT
                user_id,
                MAX(display_name) AS display_name,
                MAX(created_at) AS last_seen
            FROM message_events
            WHERE group_id = %s
              AND user_id IS NOT NULL
            GROUP BY user_id
            ORDER BY MAX(created_at) DESC
        """, (group_id,)).fetchall()

    # 프로필 캐시 또는 DB의 표시 이름이 불완전할 수 있으므로
    # 가능하면 LINE 프로필을 다시 조회합니다.
    candidates = []

    for row in rows:
        user_id = row["user_id"]
        display_name = row["display_name"] or ""

        profile = get_cached_profile(group_id, user_id)
        if profile and profile.get("displayName"):
            display_name = profile["displayName"]

        candidates.append({
            "user_id": user_id,
            "display_name": display_name,
            "normalized_name": normalize_name(display_name),
        })

    matches = {}
    problems = []

    for keyword in ADMIN_NAMES:
        normalized_keyword = normalize_name(keyword)

        found = [
            user for user in candidates
            if normalized_keyword in user["normalized_name"]
        ]

        # 같은 키워드가 표시 이름에 들어간 사용자가 여럿이면
        # 엉뚱한 사람을 멘션하지 않고 해당 키워드를 보류합니다.
        unique_users = {
            item["user_id"]: item
            for item in found
        }

        if len(unique_users) == 1:
            user = next(iter(unique_users.values()))
            matches[keyword] = user

            logger.info(
                "관리자 검색 성공 keyword=%s display_name=%s user_id=%s",
                keyword,
                user["display_name"],
                user["user_id"],
            )

        elif len(unique_users) == 0:
            problems.append(keyword)
            logger.warning(
                "관리자 검색 실패 keyword=%s: 기록된 사용자 이름에서 찾지 못했습니다.",
                keyword,
            )

        else:
            problems.append(keyword)
            logger.warning(
                "관리자 검색 중복 keyword=%s candidates=%s",
                keyword,
                [
                    {
                        "name": item["display_name"],
                        "user_id": item["user_id"],
                    }
                    for item in unique_users.values()
                ],
            )

    return matches, problems


def reply_admin_mentions(reply_token, group_id, command):
    """
    *여초 / *남초 명령을 처리합니다.
    성공적으로 식별된 관리자만 실제 LINE 멘션으로 전송합니다.
    """

    matches, problems = find_admin_users(group_id)

    if not matches:
        reply_text(
            reply_token,
            "관리자 LINE ID를 찾지 못했어요. "
            "관리자들이 이 그룹에서 메시지를 보냈는지 확인해 주세요. "
            "서버 로그에서 관리자 검색 실패 원인을 확인할 수 있어요.",
        )
        return

    substitutions = []
    text_parts = []

    for index, keyword in enumerate(ADMIN_NAMES):
        user = matches.get(keyword)
        if not user:
            continue

        placeholder = f"{{admin{index}}}"
        text_parts.append(placeholder)

        substitutions.append({
            "type": "mention",
            "mentionee": {
                "type": "user",
                "userId": user["user_id"],
            },
            "placeholder": placeholder,
        })

    if not text_parts:
        reply_text(reply_token, "멘션할 관리자를 찾지 못했어요.")
        return

    prefix = "여초 관리자 호출!\n" if command == "*여초" else "남초 관리자 호출!\n"
    suffix = "\n여초1" if command == "*여초" else "\n남초1"
    text = prefix + " ".join(text_parts) + suffix

    try:
        reply_text_v2(reply_token, text, substitutions)

        if problems:
            logger.warning(
                "관리자 멘션 일부 누락 command=%s missing_or_ambiguous=%s",
                command,
                problems,
            )

    except Exception:
        # LINE이 replyToken을 이미 소비했을 수 있으므로 같은 토큰으로 재전송하지 않습니다.
        logger.exception("실제 관리자 멘션 전송 실패")


# =========================================================
# 마딧수 랭킹
# =========================================================

def parse_date_range(text):
    match = re.fullmatch(
        r"\s*\*(\d{4}-\d{2}-\d{2})\s*~\s*"
        r"(\d{4}-\d{2}-\d{2})\s*마딧수\s*",
        text,
    )

    if not match:
        return None

    try:
        start_date = date.fromisoformat(match.group(1))
        end_date = date.fromisoformat(match.group(2))
    except ValueError:
        return None

    if end_date < start_date:
        return None

    if (end_date - start_date).days > 365:
        return None

    return start_date, end_date


def get_ranking(group_id, start_date, end_date):
    start_dt = datetime.combine(
        start_date,
        datetime.min.time(),
        tzinfo=KST,
    )
    end_dt = datetime.combine(
        end_date + timedelta(days=1),
        datetime.min.time(),
        tzinfo=KST,
    )

    with get_db() as conn:
        rows = conn.execute("""
            SELECT
                user_id,
                MAX(NULLIF(NULLIF(display_name, user_id), '')) AS display_name,
                COUNT(*) AS message_count
            FROM message_events
            WHERE group_id = %s
              AND message_type = 'text'
              AND created_at >= %s
              AND created_at < %s
            GROUP BY user_id
            ORDER BY COUNT(*) DESC, LOWER(MAX(NULLIF(display_name, user_id))) ASC
            LIMIT 30
        """, (group_id, start_dt, end_dt)).fetchall()

    return rows


def format_ranking(rows, start_date, end_date):
    if not rows:
        return "해당 기간에는 집계된 마딧수가 없어요."

    if start_date == end_date:
        title = f"📊 {start_date.isoformat()} 마딧수 랭킹"
    else:
        title = (
            f"📊 {start_date.isoformat()} ~ "
            f"{end_date.isoformat()} 마딧수 랭킹"
        )

    lines = [title, ""]

    for index, row in enumerate(rows, start=1):
        name = row["display_name"] or "이름 확인 필요"
        count = row["message_count"]
        lines.append(f"{index}위. {name} — {count}마디")

    return "\n".join(lines)


# =========================================================
# AI 대화 기록
# =========================================================

def get_history_key(event):
    source = event.get("source") or {}
    group_id = source.get("groupId") or source.get("roomId") or "private"
    user_id = source.get("userId") or "unknown"
    return group_id, user_id


def get_conversation_history(key):
    with history_lock:
        return list(conversation_histories.get(key, []))


def save_conversation_turn(key, user_text, assistant_text):
    with history_lock:
        history = conversation_histories.setdefault(key, [])
        history.append({
            "role": "user",
            "parts": [{"text": user_text}],
        })
        history.append({
            "role": "model",
            "parts": [{"text": assistant_text}],
        })

        max_messages = MAX_HISTORY_TURNS * 2
        if len(history) > max_messages:
            conversation_histories[key] = history[-max_messages:]


# =========================================================
# 웹 검색
# =========================================================

def should_search_web(text):
    lowered = text.lower()
    return any(keyword.lower() in lowered for keyword in SEARCH_KEYWORDS)


def tavily_search(query):
    if not TAVILY_API_KEY:
        return ""

    try:
        response = requests.post(
            "https://api.tavily.com/search",
            json={
                "api_key": TAVILY_API_KEY,
                "query": query,
                "search_depth": "basic",
                "max_results": 5,
                "include_answer": True,
            },
            timeout=4,
        )

        if not response.ok:
            logger.warning(
                "Tavily 검색 실패 status=%s body=%s",
                response.status_code,
                response.text[:500],
            )
            return ""

        data = response.json()
        parts = []

        answer = data.get("answer")
        if answer:
            parts.append(f"검색 요약: {answer}")

        for result in data.get("results", []):
            title = result.get("title", "")
            content = result.get("content", "")
            url = result.get("url", "")

            if content:
                parts.append(
                    f"제목: {title}\n내용: {content}\n주소: {url}"
                )

        return "\n\n".join(parts)[:9000]

    except Exception:
        logger.exception("Tavily 검색 오류")
        return ""


def build_search_context(user_text):
    if not should_search_web(user_text):
        return ""

    result = tavily_search(user_text)
    if not result:
        return ""

    return (
        "\n\n아래는 웹 검색 결과다. 검색 결과를 참고하되 "
        "검색 결과에 없는 정보를 지어내지 마라.\n"
        f"{result}"
    )


# =========================================================
# Gemini AI
# =========================================================

def get_gemini_models():
    configured = os.getenv("GEMINI_MODEL", "").strip()

    models = []
    if configured:
        models.append(configured)

    for model in DEFAULT_GEMINI_MODELS:
        if model not in models:
            models.append(model)

    return models


def parse_retry_seconds(response):
    try:
        data = response.json()
        message = (
            data.get("error", {}).get("message", "")
            if isinstance(data, dict)
            else ""
        )

        match = re.search(
            r"retry in ([0-9]+(?:\.[0-9]+)?)s",
            message,
            re.IGNORECASE,
        )

        if match:
            return max(1, int(float(match.group(1))) + 1)

    except Exception:
        pass

    return 60


def clean_answer(text):
    text = str(text or "").strip()
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text[:4500]


def gemini_answer(user_text, event):
    if not GEMINI_API_KEY:
        return "AI API 키가 설정되지 않았어요. Render 환경변수를 확인해 주세요."

    key = get_history_key(event)
    history = get_conversation_history(key)
    search_context = build_search_context(user_text)

    source = event.get("source") or {}
    group_id = source.get("groupId") or source.get("roomId")
    user_id = source.get("userId")

    display_name = "사용자"
    if group_id and user_id:
        profile = get_cached_profile(group_id, user_id)
        if profile and profile.get("displayName"):
            display_name = profile["displayName"]

    prompt = (
        f"현재 대화 상대의 표시 이름: {display_name}\n"
        f"사용자 메시지: {user_text}"
        f"{search_context}"
    )

    contents = list(history)
    contents.append({
        "role": "user",
        "parts": [{"text": prompt}],
    })

    last_error = None

    # 응답 토큰이 만료되기 전에 끝내기 위해 한 번에 최대 두 모델만 시도합니다.
    for model in get_gemini_models()[:2]:
        with cooldown_lock:
            cooldown_until = model_cooldowns.get(model, 0)

        if cooldown_until > time.time():
            logger.info(
                "Gemini 모델 대기 중 model=%s seconds=%s",
                model,
                int(cooldown_until - time.time()),
            )
            continue

        url = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"{model}:generateContent"
        )

        payload = {
            "systemInstruction": {
                "parts": [{"text": SYSTEM_PROMPT}],
            },
            "contents": contents,
            "generationConfig": {
                "maxOutputTokens": 800,
                "temperature": 0.7,
            },
        }

        try:
            response = requests.post(
                url,
                headers={
                    "x-goog-api-key": GEMINI_API_KEY,
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=GEMINI_REQUEST_TIMEOUT,
            )

            if response.status_code == 429:
                retry_seconds = parse_retry_seconds(response)

                with cooldown_lock:
                    model_cooldowns[model] = (
                        time.time() + retry_seconds
                    )

                logger.warning(
                    "Gemini 사용량 제한 model=%s retry=%s",
                    model,
                    retry_seconds,
                )
                last_error = f"{model}: 429"
                continue

            if response.status_code in (400, 403, 404):
                logger.warning(
                    "Gemini 모델 사용 불가 model=%s status=%s body=%s",
                    model,
                    response.status_code,
                    response.text[:800],
                )
                with cooldown_lock:
                    model_cooldowns[model] = time.time() + MODEL_COOLDOWN_SECONDS
                last_error = f"{model}: {response.status_code}"
                continue

            if response.status_code >= 500:
                logger.warning(
                    "Gemini 일시 오류 model=%s status=%s body=%s",
                    model, response.status_code, response.text[:500],
                )
                with cooldown_lock:
                    model_cooldowns[model] = time.time() + MODEL_COOLDOWN_SECONDS
                last_error = f"{model}: {response.status_code}"
                continue

            if not response.ok:
                logger.warning(
                    "Gemini 응답 오류 model=%s status=%s body=%s",
                    model,
                    response.status_code,
                    response.text[:800],
                )
                last_error = f"{model}: {response.status_code}"
                continue

            data = response.json()
            candidates = data.get("candidates", [])

            if not candidates:
                last_error = f"{model}: candidates 없음"
                continue

            candidate = candidates[0]
            finish_reason = candidate.get("finishReason", "")
            parts = (
                candidate.get("content", {}).get("parts", [])
            )

            answer = "\n".join(
                part.get("text", "")
                for part in parts
                if part.get("text")
            ).strip()

            # 잘린 답변은 긴 재요청으로 replyToken 시간을 소모하지 않고
            # 다음 모델로 넘어갑니다.
            if finish_reason == "MAX_TOKENS":
                logger.warning("Gemini 답변 길이 제한 model=%s", model)
                last_error = f"{model}: 답변 길이 제한"
                continue

            answer = clean_answer(answer)

            if not answer:
                last_error = f"{model}: 빈 답변"
                continue

            save_conversation_turn(key, user_text, answer)

            logger.info("Gemini 답변 성공 model=%s", model)
            return answer

        except requests.RequestException as exc:
            logger.exception("Gemini 요청 실패 model=%s", model)
            last_error = str(exc)
            continue

        except Exception as exc:
            logger.exception("Gemini 처리 오류 model=%s", model)
            last_error = str(exc)
            continue

    logger.error("모든 Gemini 모델 실패: %s", last_error)

    return (
        "지금은 AI 응답을 가져오지 못했어요. "
        "잠시 후 다시 시도해 주세요."
    )


# =========================================================
# 메시지 처리
# =========================================================

def handle_text(event):
    source = event.get("source") or {}
    message = event.get("message") or {}

    text = (message.get("text") or "").strip()
    reply_token = event.get("replyToken")

    group_id = source.get("groupId") or source.get("roomId")

    if not text or not reply_token:
        return

    # 관리자 호출 명령
    if text in ("*여초", "*남초"):
        reply_admin_mentions(reply_token, group_id, text)
        return

    # 오늘 마딧수
    if text == "*마딧수":
        today = datetime.now(KST).date()

        rows = get_ranking(
            group_id,
            today,
            today,
        )

        reply_text(
            reply_token,
            format_ranking(rows, today, today),
        )
        return

    # 기간별 마딧수
    date_range = parse_date_range(text)

    if date_range:
        start_date, end_date = date_range

        rows = get_ranking(
            group_id,
            start_date,
            end_date,
        )

        reply_text(
            reply_token,
            format_ranking(rows, start_date, end_date),
        )
        return

    # AI는 !로 시작하는 메시지에만 반응
    if text.startswith("!"):
        user_text = text[1:].strip()

        if not user_text:
            reply_text(
                reply_token,
                "질문을 입력해 주세요. 예: !오늘 날씨 알려줘",
            )
            return

        started = time.time()
        answer = gemini_answer(user_text, event)
        logger.info("AI 처리 시간 %.2f초", time.time() - started)
        reply_text(reply_token, answer)
        return

    # 그 외 메시지는 기록만 하고 AI가 답하지 않습니다.


# =========================================================
# Flask 라우트
# =========================================================

@app.route("/", methods=["GET"])
def home():
    return "TheColor LINE Bot is running!"


@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "status": "ok",
        "service": "thecolor-line-bot",
    })


@app.route("/callback", methods=["POST"])
def callback():
    body = request.get_data()
    signature = request.headers.get("X-Line-Signature", "")

    if not verify_signature(body, signature):
        logger.warning("LINE 서명 검증 실패")
        abort(400)

    try:
        payload = request.get_json(force=True)
        events = payload.get("events", [])

        for event in events:
            if event.get("type") != "message":
                continue

            source = event.get("source") or {}
            message = event.get("message") or {}

            # 개인 채팅이 아니라 그룹/방 이벤트만 처리
            group_id = source.get("groupId") or source.get("roomId")
            if not group_id:
                continue

            # 모든 메시지를 저장해 사용자 ID와 표시 이름을 축적
            try:
                record_message(event)
            except Exception:
                logger.exception("메시지 DB 저장 실패")
                # 저장이 실패해도 다른 이벤트 처리 시도

            # 텍스트 메시지만 명령/AI 처리
            if message.get("type") == "text":
                try:
                    handle_text(event)
                except Exception:
                    logger.exception("메시지 처리 실패")

        return "OK", 200

    except Exception:
        logger.exception("Webhook 처리 실패")
        return "OK", 200


# =========================================================
# 실행
# =========================================================

if __name__ == "__main__":
    ensure_database()
    app.run(
        host="0.0.0.0",
        port=int(os.getenv("PORT", "10000")),
    )
