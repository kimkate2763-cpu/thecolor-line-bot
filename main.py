
import os
import re
import hmac
import hashlib
import logging
import base64
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
from threading import Lock

import requests
import psycopg
from psycopg.rows import dict_row
from flask import Flask, request, abort, jsonify

app = Flask(__name__)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("thecolor-line-bot")

KST = ZoneInfo("Asia/Seoul")

LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET", "")
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
DATABASE_URL = os.getenv("DATABASE_URL", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "").strip()
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY", "").strip()

ADMIN_NAMES = [
    "무무", "이리", "타워", "렌치", "절도",
    "주음", "꼬꼬", "짜필", "냠냠",
]

# 최근 대화를 사용자별로 기억합니다.
# 10턴 = 사용자 질문 10개 + AI 답변 최대 10개
MAX_HISTORY_TURNS = 10
conversation_histories = {}
history_lock = Lock()

# 프로필 캐시
profile_cache = {}
PROFILE_CACHE_SECONDS = 21600

# 한도 초과 모델의 재요청을 줄이기 위한 임시 기록
# 프로세스가 재시작되면 초기화됩니다.
model_cooldowns = {}
cooldown_lock = Lock()

SYSTEM_PROMPT = """
너는 LINE 단체방 '더컬러'에서 활동하는 레드봇 2.0이야.
너는 AI 챗봇이며, 자신을 AI 집사나 다른 봇으로 소개하지 마.
정체를 물으면 '나는 더컬러의 레드봇 2.0이야'라고 알려 줘.

[대화 방식]
- 자연스럽고 친근한 한국어로 대화해.
- 딱딱한 상담원이나 검색 요약기처럼 말하지 마.
- 상대방의 말투와 분위기를 적절히 맞춰.
- 가벼운 잡담, 심심하다는 말, 메뉴 추천, 취향 이야기,
  고민 상담, 감정 표현, 장난스러운 대화에도 자연스럽게 반응해.
- 사용자가 앞서 말한 내용을 고려해 대화를 이어가.
- 이전 대화에서 이미 답한 내용을 무시하거나 같은 질문을
  반복하지 마.
- 매번 답변 끝에 억지로 질문을 붙이지 마.
- 과장된 칭찬, 지나친 위로, 뻔한 문구를 반복하지 마.
- 사용자가 속상한 일을 말하면 먼저 감정을 이해하고,
  상황에 맞는 반응이나 실질적인 도움을 제공해.
- 필요 이상으로 긴 서론이나 결론을 쓰지 마.
- 일반적인 답변은 자연스러운 길이로 작성하되,
  설명이 필요한 질문은 충분히 설명해.
- 짧은 질문이라고 무조건 한 문장으로 끝내지 마.
- 사용자가 후속 질문을 하면 이전 대화의 맥락을 반영해.

[정확성]
- 확인하지 못한 사실이나 규칙을 지어내지 마.
- 모르는 것은 아는 척하지 말고 솔직하게 알려 줘.
- 최신 정보가 필요한 질문은 제공된 검색 자료를 참고해.
- 검색 자료가 없으면 최신 정보를 확인했다고 주장하지 마.
- 사용자가 제공한 정보를 사실로 단정하기 전에 맥락을 살펴.
- 더컬러의 확인되지 않은 방 규칙을 임의로 만들지 마.

[정체성 및 범위]
- 시크릿 펜트리의 방 규칙, 탐험 시스템, 게임 설명,
  기존 집사 설정을 가져오지 마.
- 더컬러에서 확인되지 않은 기능을 실제로 존재한다고 말하지 마.
- 답변은 사용자의 질문과 대화 흐름에 맞춰 작성해.
"""

# 기존 모델 목록을 유지합니다.
# 실제 사용 가능 여부와 무료 한도는 Google AI Studio에서 확인해야 합니다.
GEMINI_MODEL_FALLBACKS = [
    "gemini-3.8-flash",
    "gemini-3.7-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
]

TAVILY_SEARCH_URL = "https://api.tavily.com/search"

SEARCH_KEYWORDS = [
    "최신", "오늘", "현재", "요즘", "최근", "지금",
    "뉴스", "날씨", "환율", "주가", "가격", "시세",
    "유행", "검색", "찾아줘", "알아봐", "정보 알려",
    "영업시간", "운영시간", "이번 주", "이번달",
    "올해", "어제", "내일", "출처", "근황",
]

_db_initialized = False


def get_gemini_models():
    models = []

    if GEMINI_MODEL:
        models.append(GEMINI_MODEL)

    models.extend(GEMINI_MODEL_FALLBACKS)
    return list(dict.fromkeys(models))


def should_search_web(user_text):
    text = user_text.lower()
    return any(keyword in text for keyword in SEARCH_KEYWORDS)


def tavily_search(query):
    if not TAVILY_API_KEY:
        logger.info("TAVILY_API_KEY 미설정: 웹 검색 생략")
        return []

    try:
        response = requests.post(
            TAVILY_SEARCH_URL,
            json={
                "api_key": TAVILY_API_KEY,
                "query": query[:400],
                "search_depth": "basic",
                "max_results": 5,
                "include_answer": False,
                "include_raw_content": False,
            },
            timeout=8,
        )

        if not response.ok:
            logger.warning(
                "Tavily 검색 실패: HTTP %s %s",
                response.status_code,
                response.text[:300],
            )
            return []

        data = response.json()
        cleaned = []

        for item in data.get("results", [])[:5]:
            title = str(item.get("title", "")).strip()
            url = str(item.get("url", "")).strip()
            content = str(item.get("content", "")).strip()

            if content:
                cleaned.append({
                    "title": title[:200],
                    "url": url,
                    "content": content[:1500],
                })

        return cleaned

    except (requests.exceptions.RequestException, ValueError, TypeError):
        logger.exception("Tavily 검색 오류")
        return []


def build_search_context(results):
    if not results:
        return "", []

    lines = ["웹 검색 결과:"]
    sources = []

    for index, item in enumerate(results, start=1):
        lines.append(
            f"\n[{index}] {item['title']}\n{item['content']}"
        )

        url = item["url"]
        if url.startswith(("https://", "http://")):
            sources.append((item["title"] or "참고 자료", url))

    return "\n".join(lines), sources


def get_db():
    if not DATABASE_URL:
        raise RuntimeError(
            "DATABASE_URL 환경 변수가 설정되지 않았습니다."
        )

    return psycopg.connect(
        DATABASE_URL,
        row_factory=dict_row,
        connect_timeout=10,
    )


def init_database():
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS message_events (
                event_id TEXT PRIMARY KEY,
                group_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                display_name TEXT NOT NULL,
                message_type TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)

        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_message_events_group_date
            ON message_events (group_id, created_at)
        """)

        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_message_events_user_date
            ON message_events (group_id, user_id, created_at)
        """)


def ensure_database():
    global _db_initialized

    if _db_initialized:
        return

    init_database()
    _db_initialized = True


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


def line_api(method, endpoint, payload=None, timeout=8):
    if not LINE_CHANNEL_ACCESS_TOKEN:
        raise RuntimeError(
            "LINE access token이 설정되지 않았습니다."
        )

    response = requests.request(
        method,
        "https://api.line.me" + endpoint,
        headers={
            "Authorization": "Bearer " + LINE_CHANNEL_ACCESS_TOKEN,
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=timeout,
    )

    response.raise_for_status()

    if response.content:
        return response.json()

    return {}


def reply_text(reply_token, text):
    if not reply_token:
        return

    text = str(text)[:4500]

    line_api(
        "POST",
        "/v2/bot/message/reply",
        {
            "replyToken": reply_token,
            "messages": [{"type": "text", "text": text}],
        },
    )


def get_member_profile(group_id, user_id, source_type):
    if source_type == "group":
        endpoint = f"/v2/bot/group/{group_id}/member/{user_id}"
    elif source_type == "room":
        endpoint = f"/v2/bot/room/{group_id}/member/{user_id}"
    else:
        return None

    try:
        return line_api("GET", endpoint)
    except Exception:
        logger.exception("멤버 프로필 조회 실패")
        return None


def get_cached_profile(group_id, user_id, source_type):
    key = (group_id, user_id)
    now = time.time()

    cached = profile_cache.get(key)

    if cached and now - cached["time"] < PROFILE_CACHE_SECONDS:
        return cached["name"]

    profile = get_member_profile(group_id, user_id, source_type)
    name = (profile or {}).get("displayName")

    if name:
        profile_cache[key] = {
            "name": name,
            "time": now,
        }
        return name

    if cached:
        return cached["name"]

    return "이름 확인 불가"


def record_message(event, source, message):
    group_id = source.get("groupId") or source.get("roomId")
    user_id = source.get("userId")

    if not group_id or not user_id:
        return

    ensure_database()

    display_name = get_cached_profile(
        group_id,
        user_id,
        source.get("type", ""),
    )

    event_id = event.get("webhookEventId") or message.get("id")

    if not event_id:
        event_id = hashlib.sha256(
            (
                str(event.get("timestamp", ""))
                + group_id + user_id
                + str(message.get("id", ""))
            ).encode("utf-8")
        ).hexdigest()

    timestamp = event.get("timestamp")
    created_at = (
        datetime.fromtimestamp(timestamp / 1000, tz=KST)
        if timestamp
        else datetime.now(KST)
    )

    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO message_events
                (event_id, group_id, user_id, display_name,
                 message_type, created_at)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (event_id) DO NOTHING
            """,
            (
                str(event_id), group_id, user_id, display_name,
                message.get("type", "unknown"), created_at,
            ),
        )


def get_admin_user_ids(group_id):
    ensure_database()

    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT ON (user_id)
                   user_id, display_name
            FROM message_events
            WHERE group_id = %s
            ORDER BY user_id, created_at DESC
            """,
            (group_id,),
        ).fetchall()

    ids_by_name = {}

    for row in rows:
        name = (row["display_name"] or "").strip()
        user_id = row["user_id"]

        if name and name != "이름 확인 불가":
            ids_by_name.setdefault(name, set()).add(user_id)

    resolved = []
    missing = []
    ambiguous = []

    for name in ADMIN_NAMES:
        ids = ids_by_name.get(name, set())

        if not ids:
            missing.append(name)
        elif len(ids) > 1:
            ambiguous.append(name)
        else:
            resolved.append((name, next(iter(ids))))

    return resolved, missing, ambiguous


def reply_admin_mentions(reply_token, group_id, suffix):
    if not reply_token:
        return

    resolved, missing, ambiguous = get_admin_user_ids(group_id)

    if missing or ambiguous or len(resolved) != len(ADMIN_NAMES):
        lines = ["관리자 ID 확인 결과"]

        if resolved:
            lines.append(
                "DB에서 확인된 이름: "
                + ", ".join(name for name, _ in resolved)
            )

        if missing:
            lines.append(
                "DB에서 찾지 못한 이름: "
                + ", ".join(missing)
            )

        if ambiguous:
            lines.append(
                "이름이 중복된 항목: "
                + ", ".join(ambiguous)
            )

        lines.append(
            "누락된 관리자가 이 방에서 메시지를 보냈는지, "
            "LINE 프로필 이름이 명단과 정확히 일치하는지 확인해 주세요."
        )

        reply_text(reply_token, "\n".join(lines))
        return

    substitutions = {}
    mention_text = []

    for index, (name, user_id) in enumerate(resolved, start=1):
        key = f"admin{index}"
        mention_text.append("{" + key + "}")

        substitutions[key] = {
            "type": "mention",
            "mentionee": {
                "type": "user",
                "userId": user_id,
            },
        }

    message_text = " ".join(mention_text) + "\n" + suffix

    line_api(
        "POST",
        "/v2/bot/message/reply",
        {
            "replyToken": reply_token,
            "messages": [{
                "type": "textV2",
                "text": message_text,
                "substitution": substitutions,
            }],
        },
    )

    logger.info(
        "관리자 멘션 전송 성공: group=%s suffix=%s",
        group_id,
        suffix,
    )


def parse_date_range(text):
    match = re.search(
        r"\*(\d{4}-\d{2}-\d{2})\s*~\s*"
        r"(\d{4}-\d{2}-\d{2})\s*마딧수",
        text.strip(),
    )

    if not match:
        return None

    try:
        start = date.fromisoformat(match.group(1))
        end = date.fromisoformat(match.group(2))
    except ValueError:
        return None

    if end < start or (end - start).days > 366:
        return None

    return start, end


def get_ranking(group_id, start, end):
    ensure_database()

    start_dt = datetime.combine(
        start, datetime.min.time(), tzinfo=KST
    )
    end_dt = datetime.combine(
        end + timedelta(days=1),
        datetime.min.time(),
        tzinfo=KST,
    )

    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT user_id, display_name, COUNT(*) AS message_count
            FROM message_events
            WHERE group_id = %s
              AND created_at >= %s
              AND created_at < %s
            GROUP BY user_id, display_name
            ORDER BY message_count DESC, display_name ASC
            LIMIT 30
            """,
            (group_id, start_dt, end_dt),
        ).fetchall()

    return rows


def format_ranking(rows, start, end):
    if not rows:
        return f"{start}~{end} 기간에는 기록된 마딧수가 없어요."

    lines = [f"📊 마딧수 순위 ({start} ~ {end})"]

    for index, row in enumerate(rows, start=1):
        lines.append(
            f"{index}위. {row['display_name']} — "
            f"{row['message_count']}마디"
        )

    return "\n".join(lines)


def get_history_key(group_id, user_id):
    return (group_id, user_id)


def get_conversation_history(group_id, user_id):
    key = get_history_key(group_id, user_id)

    with history_lock:
        return list(conversation_histories.get(key, []))


def save_conversation_turn(group_id, user_id, question, answer):
    key = get_history_key(group_id, user_id)

    with history_lock:
        history = conversation_histories.setdefault(key, [])

        history.append({
            "role": "user",
            "parts": [{"text": question[:3500]}],
        })
        history.append({
            "role": "model",
            "parts": [{"text": answer[:4500]}],
        })

        max_messages = MAX_HISTORY_TURNS * 2
        if len(history) > max_messages:
            del history[:-max_messages]


def parse_retry_seconds(response_text):
    match = re.search(
        r"retry in\s+"
        r"(?:(\d+)h)?\s*"
        r"(?:(\d+)m)?\s*"
        r"(?:(\d+(?:\.\d+)?)s)?",
        response_text,
        re.IGNORECASE,
    )

    if not match:
        return 3600

    hours = int(match.group(1) or 0)
    minutes = int(match.group(2) or 0)
    seconds = float(match.group(3) or 0)

    return max(60, int(hours * 3600 + minutes * 60 + seconds))


def gemini_answer(user_text, group_id, user_id, display_name=""):
    if not GEMINI_API_KEY:
        logger.error("GEMINI_API_KEY가 설정되지 않았습니다.")
        return (
            "아직 레드봇 2.0의 AI 연결이 완료되지 않았어요. "
            "관리자가 Gemini API 키 설정을 확인해 주세요."
        )

    needs_search = should_search_web(user_text)
    search_results = tavily_search(user_text) if needs_search else []
    search_context, sources = build_search_context(search_results)

    prompt = user_text[:3500]

    if search_context:
        prompt += (
            "\n\n[웹 검색 참고 자료]\n"
            + search_context[:8000]
            + "\n\n검색 자료를 근거로 답변하고, 자료에 없는 내용은 "
            "확실하다고 단정하지 마세요."
        )
    elif needs_search:
        prompt += (
            "\n\n웹 검색 결과를 가져오지 못했습니다. "
            "최신 정보를 확인한 것처럼 말하지 마세요."
        )

    system_prompt = SYSTEM_PROMPT

    if display_name and display_name != "이름 확인 불가":
        system_prompt += (
            "\n현재 대화 상대의 LINE 표시 이름은 "
            + display_name[:80]
            + "입니다. 이름을 부르는 것이 자연스러울 때만 사용하세요."
        )

    # 현재 사용자의 최근 대화 기록을 가져옵니다.
    history = get_conversation_history(group_id, user_id)

    contents = history + [{
        "role": "user",
        "parts": [{"text": prompt}],
    }]

    now = time.time()

    with cooldown_lock:
        available_models = [
            model for model in get_gemini_models()
            if model_cooldowns.get(model, 0) <= now
        ]

    if not available_models:
        logger.warning("사용 가능한 Gemini 모델이 모두 대기 상태입니다.")
        return (
            "AI 사용 한도가 잠시 막혀 있어요. 😿\n"
            "무료 한도가 회복된 뒤 다시 질문해 주세요."
        )

    last_error = None
    saw_quota_error = False

    for model in available_models:
        url = (
            "https://generativelanguage.googleapis.com/"
            "v1beta/models/" + model + ":generateContent"
        )

        try:
            logger.info(
                "Gemini 모델 시도: %s, 이전 대화 메시지 수: %s",
                model,
                len(history),
            )

            response = requests.post(
                url,
                headers={
                    "x-goog-api-key": GEMINI_API_KEY,
                    "Content-Type": "application/json",
                },
                json={
                    "systemInstruction": {
                        "parts": [{"text": system_prompt}]
                    },
                    "contents": contents,
                    "generationConfig": {
                        "maxOutputTokens": 700
                    },
                },
                timeout=30,
            )

            if not response.ok:
                last_error = (
                    f"{model}: HTTP {response.status_code} "
                    f"{response.text[:500]}"
                )
                logger.warning("Gemini 모델 실패: %s", last_error)

                if response.status_code == 429:
                    wait_seconds = parse_retry_seconds(response.text)

                    with cooldown_lock:
                        model_cooldowns[model] = (
                            time.time() + wait_seconds
                        )

                    saw_quota_error = True

                    logger.warning(
                        "모델 %s 한도 초과: 약 %s초 대기 후 재시도 가능",
                        model,
                        wait_seconds,
                    )
                    continue

                continue

            data = response.json()
            candidates = data.get("candidates", [])

            if not candidates:
                last_error = f"{model}: 응답 후보 없음"
                logger.warning("%s", last_error)
                continue

            candidate = candidates[0]
            parts = candidate.get("content", {}).get("parts", [])

            answer = "\n".join(
                part.get("text", "")
                for part in parts
                if part.get("text")
            ).strip()

            if not answer:
                last_error = f"{model}: 답변 텍스트 없음"
                continue

            if sources:
                source_lines = []
                seen_urls = set()

                for title, uri in sources:
                    if uri in seen_urls:
                        continue

                    seen_urls.add(uri)
                    source_lines.append(f"• {title}\n{uri}")

                    if len(source_lines) >= 3:
                        break

                if source_lines:
                    answer += "\n\n🔎 참고 출처\n"
                    answer += "\n".join(source_lines)

            # 성공한 답변만 대화 기록에 저장합니다.
            # 검색 결과 원문이 아니라 사용자의 질문과 최종 답변을 저장합니다.
            save_conversation_turn(
                group_id,
                user_id,
                user_text,
                answer,
            )

            logger.info("Gemini 답변 성공: %s", model)
            return answer

        except requests.exceptions.RequestException as exc:
            last_error = f"{model}: 통신 오류: {str(exc)}"
            logger.exception("Gemini 통신 실패: %s", model)
            continue

        except (KeyError, ValueError, TypeError) as exc:
            last_error = f"{model}: 응답 처리 오류: {str(exc)}"
            logger.exception("Gemini 응답 처리 실패: %s", model)
            continue

        except Exception as exc:
            last_error = f"{model}: 예기치 않은 오류: {str(exc)}"
            logger.exception("Gemini 모델 처리 실패: %s", model)
            continue

    logger.error("Gemini 모델 요청 실패: %s", last_error)

    if saw_quota_error:
        return (
            "AI 사용 한도에 도달했어요. 😿\n"
            "사용 가능한 무료 모델이 없어 잠시 후 다시 시도해 주세요."
        )

    return (
        "답변을 가져오지 못했어요. 😿\n"
        "관리자가 Render 로그에서 오류를 확인해 주세요."
    )


def handle_text(text, event, source):
    group_id = source.get("groupId") or source.get("roomId")
    user_id = source.get("userId")
    reply_token = event.get("replyToken")
    stripped = text.strip()

    if not group_id:
        return

    # 기존 마딧수 명령어
    if stripped == "*마딧수":
        today = datetime.now(KST).date()

        try:
            rows = get_ranking(group_id, today, today)
            reply_text(reply_token, format_ranking(rows, today, today))
        except Exception:
            logger.exception("오늘 마딧수 조회 실패")
            reply_text(
                reply_token,
                "마딧수 조회 중 문제가 생겼어요. 잠시 후 다시 시도해 주세요.",
            )
        return

    date_range = parse_date_range(stripped)

    if date_range:
        start, end = date_range

        try:
            rows = get_ranking(group_id, start, end)
            reply_text(reply_token, format_ranking(rows, start, end))
        except Exception:
            logger.exception("기간별 마딧수 조회 실패")
            reply_text(
                reply_token,
                "마딧수 조회 중 문제가 생겼어요. 잠시 후 다시 시도해 주세요.",
            )
        return

    # 기존 관리자 멘션 기능
    if stripped == "*여초":
        reply_admin_mentions(reply_token, group_id, "여초1")
        return

    if stripped == "*남초":
        reply_admin_mentions(reply_token, group_id, "남초1")
        return

    # 기존처럼 !로 시작하는 질문에만 AI가 답변합니다.
    if stripped.startswith("!"):
        question = stripped[1:].strip()

        if not question:
            return

        if not user_id:
            reply_text(
                reply_token,
                "사용자 정보를 확인하지 못했어요. 다시 시도해 주세요.",
            )
            return

        display_name = get_cached_profile(
            group_id,
            user_id,
            source.get("type", ""),
        )

        answer = gemini_answer(
            question,
            group_id,
            user_id,
            display_name,
        )
        reply_text(reply_token, answer)
        return


@app.get("/")
def health():
    return jsonify({
        "status": "ok",
        "service": "thecolor-line-bot",
        "identity": "레드봇 2.0",
    })


@app.get("/health")
def health_check():
    return jsonify({
        "status": "healthy",
        "identity": "레드봇 2.0",
    })


@app.post("/callback")
def callback():
    body = request.get_data()
    signature = request.headers.get("X-Line-Signature", "")

    if not verify_signature(body, signature):
        abort(400)

    payload = request.get_json(silent=True) or {}

    for event in payload.get("events", []):
        if event.get("type") != "message":
            continue

        source = event.get("source", {})
        message = event.get("message", {})

        try:
            record_message(event, source, message)
        except Exception:
            logger.exception("메시지 기록 실패")

        if message.get("type") == "text":
            try:
                handle_text(
                    message.get("text", ""),
                    event,
                    source,
                )
            except Exception:
                logger.exception("메시지 답변 처리 실패")

    return "OK", 200


# Render의 Gunicorn이 app 객체를 실행합니다.
