
import os
import re
import hmac
import hashlib
import logging
import base64
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

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

KEYWORDS = [
    "무무", "이리", "타워", "랜치",
    "절도", "주음", "꼬꼬", "짜필"
]

SYSTEM_PROMPT = """
너는 LINE 단체방 '더컬러'의 친근하고 다정한 AI 집사야.
자연스러운 한국어 존댓말을 사용해.
답변은 보통 1~4문장으로 간결하게 작성해.
사용자의 감정을 먼저 이해하고, 필요할 때 실질적인 도움을 줘.
확인하지 못한 방 규칙이나 사실을 지어내지 마.
날씨, 최신 소식, 유행, 현재 정보처럼 최신성이 필요한 질문은
Google 검색 결과를 바탕으로 답변해.
검색으로 확인되지 않은 정보는 사실처럼 단정하지 마.
"""

# 지정한 모델을 먼저 시도하고, 실패하면 다음 모델로 넘어갑니다.
# 모델별 사용 가능 여부는 API 키와 Google AI Studio 프로젝트에 따라 달라집니다.
GEMINI_MODEL_FALLBACKS = [
    "gemini-3.8-flash",
    "gemini-3.7-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-2.5-flash",
]


def get_gemini_models():
    models = []

    if GEMINI_MODEL:
        models.append(GEMINI_MODEL)

    models.extend(GEMINI_MODEL_FALLBACKS)

    # 중복 모델 제거
    return list(dict.fromkeys(models))


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


_db_initialized = False


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


def line_api(method, endpoint, payload=None):
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
        timeout=15,
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
            "messages": [
                {
                    "type": "text",
                    "text": text,
                }
            ],
        },
    )


def get_member_profile(group_id, user_id, source_type):
    if source_type == "group":
        endpoint = (
            f"/v2/bot/group/{group_id}/member/{user_id}"
        )
    elif source_type == "room":
        endpoint = (
            f"/v2/bot/room/{group_id}/member/{user_id}"
        )
    else:
        return None

    try:
        return line_api("GET", endpoint)
    except Exception:
        logger.exception("멤버 프로필 조회 실패")
        return None


def record_message(event, source, message):
    group_id = source.get("groupId") or source.get("roomId")
    user_id = source.get("userId")

    if not group_id or not user_id:
        return

    ensure_database()

    source_type = source.get("type", "")
    profile = get_member_profile(
        group_id,
        user_id,
        source_type,
    )

    display_name = (
        (profile or {}).get("displayName")
        or "이름 확인 불가"
    )

    event_id = event.get("webhookEventId")

    if not event_id:
        event_id = message.get("id")

    if not event_id:
        event_id = hashlib.sha256(
            (
                str(event.get("timestamp", ""))
                + group_id
                + user_id
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
                str(event_id),
                group_id,
                user_id,
                display_name,
                message.get("type", "unknown"),
                created_at,
            ),
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

    if end < start:
        return None

    if (end - start).days > 366:
        return None

    return start, end


def get_ranking(group_id, start, end):
    ensure_database()

    start_dt = datetime.combine(
        start,
        datetime.min.time(),
        tzinfo=KST,
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
        return (
            f"{start}~{end} 기간에는 기록된 마딧수가 없어요."
        )

    lines = [f"📊 마딧수 순위 ({start} ~ {end})"]

    for index, row in enumerate(rows, start=1):
        lines.append(
            f"{index}위. {row['display_name']} — "
            f"{row['message_count']}마디"
        )

    return "\n".join(lines)


def gemini_answer(user_text):
    if not GEMINI_API_KEY:
        logger.error("GEMINI_API_KEY가 설정되지 않았습니다.")
        return (
            "아직 AI 집사 연결이 완료되지 않았어요. "
            "관리자가 Gemini API 키 설정을 확인해 주세요."
        )

    last_error = None

    for model in get_gemini_models():
        url = (
            "https://generativelanguage.googleapis.com/"
            "v1beta/models/"
            + model
            + ":generateContent"
        )

        try:
            logger.info("Gemini 모델 시도: %s", model)

            response = requests.post(
                url,
                params={"key": GEMINI_API_KEY},
                json={
                    "systemInstruction": {
                        "parts": [{"text": SYSTEM_PROMPT}]
                    },
                    "contents": [
                        {
                            "role": "user",
                            "parts": [
                                {"text": user_text[:4000]}
                            ],
                        }
                    ],
                    "tools": [
                        {
                            "google_search": {}
                        }
                    ],
                    "generationConfig": {
                        "temperature": 0.8,
                        "maxOutputTokens": 600,
                    },
                },
                timeout=40,
            )

            if not response.ok:
                last_error = (
                    f"{model}: HTTP {response.status_code} "
                    f"{response.text[:500]}"
                )
                logger.warning("Gemini 모델 실패: %s", last_error)
                continue

            data = response.json()
            candidates = data.get("candidates", [])

            if not candidates:
                last_error = f"{model}: 응답 후보 없음"
                logger.warning("%s", last_error)
                continue

            candidate = candidates[0]
            parts = candidate.get("content", {}).get(
                "parts", []
            )

            answer = "\n".join(
                part.get("text", "")
                for part in parts
                if part.get("text")
            ).strip()

            if not answer:
                last_error = f"{model}: 답변 텍스트 없음"
                logger.warning("%s", last_error)
                continue

            metadata = candidate.get(
                "groundingMetadata", {}
            )
            chunks = metadata.get("groundingChunks", [])

            source_lines = []
            seen_urls = set()

            for chunk in chunks:
                web_source = chunk.get("web", {})
                uri = web_source.get("uri", "")
                title = web_source.get("title", "")

                if (
                    uri.startswith("https://")
                    and uri not in seen_urls
                ):
                    seen_urls.add(uri)
                    source_lines.append(
                        f"• {title or '참고 자료'}\n{uri}"
                    )

                if len(source_lines) >= 3:
                    break

            if source_lines:
                answer += "\n\n🔎 참고 출처\n"
                answer += "\n".join(source_lines)

            logger.info("Gemini 답변 성공: %s", model)
            return answer

        except requests.exceptions.RequestException as exc:
            last_error = f"{model}: {str(exc)}"
            logger.exception("Gemini 통신 실패: %s", model)
            continue

        except (KeyError, ValueError, TypeError) as exc:
            last_error = f"{model}: 응답 처리 오류: {str(exc)}"
            logger.exception("Gemini 응답 처리 실패: %s", model)
            continue

        except Exception as exc:
            last_error = f"{model}: {str(exc)}"
            logger.exception("Gemini 모델 처리 실패: %s", model)
            continue

    logger.error(
        "모든 Gemini 모델 실패. 마지막 오류: %s",
        last_error,
    )

    return (
        "집사가 여러 AI 모델에 연결해 봤지만 "
        "이번에는 답변을 가져오지 못했어요. "
        "잠시 후 다시 말씀해 주세요."
    )


def handle_text(text, event, source):
    group_id = source.get("groupId") or source.get("roomId")
    reply_token = event.get("replyToken")
    stripped = text.strip()

    if not group_id:
        return

    if stripped == "*마딧수":
        today = datetime.now(KST).date()

        try:
            rows = get_ranking(group_id, today, today)
            reply_text(
                reply_token,
                format_ranking(rows, today, today),
            )
        except Exception:
            logger.exception("오늘 마딧수 조회 실패")
            reply_text(
                reply_token,
                "마딧수 조회 중 문제가 생겼어요. "
                "잠시 후 다시 시도해 주세요.",
            )

        return

    date_range = parse_date_range(stripped)

    if date_range:
        start, end = date_range

        try:
            rows = get_ranking(group_id, start, end)
            reply_text(
                reply_token,
                format_ranking(rows, start, end),
            )
        except Exception:
            logger.exception("기간별 마딧수 조회 실패")
            reply_text(
                reply_token,
                "마딧수 조회 중 문제가 생겼어요. "
                "잠시 후 다시 시도해 주세요.",
            )

        return

    if stripped in ("!여초", "!남초"):
        reply_text(
            reply_token,
            "이 명령어의 실제 멤버 검색 및 멘션 기능은 "
            "LINE 그룹 멤버 조회 권한을 확인한 뒤 연결할게요.",
        )
        return

    # !로 시작하는 질문에만 AI가 답변합니다.
    if stripped.startswith("!"):
        question = stripped[1:].strip()

        if not question:
            return

        reply_text(
            reply_token,
            gemini_answer(question),
        )
        return

    # 일반 채팅에는 답변하지 않습니다.
    return


@app.get("/")
def health():
    return jsonify({
        "status": "ok",
        "service": "thecolor-line-bot",
    })


@app.get("/health")
def health_check():
    return jsonify({"status": "healthy"})


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
