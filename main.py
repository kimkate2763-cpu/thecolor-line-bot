
import os
import re
import hmac
import hashlib
import logging
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
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")

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
"""


def get_db():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL 환경 변수가 설정되지 않았습니다.")
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
                created_at TIMESTAMPTZ NOT NULL
                    DEFAULT NOW()
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


def verify_signature(body, signature):
    if not LINE_CHANNEL_SECRET or not signature:
        return False
    digest = hmac.new(
        LINE_CHANNEL_SECRET.encode("utf-8"),
        body,
        hashlib.sha256,
    ).digest()
    import base64
    expected = base64.b64encode(digest).decode("utf-8")
    return hmac.compare_digest(expected, signature)


def line_api(method, endpoint, payload=None):
    if not LINE_CHANNEL_ACCESS_TOKEN:
        raise RuntimeError("LINE access token이 설정되지 않았습니다.")

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

    # LINE 메시지 한 건의 텍스트 길이를 안전하게 제한합니다.
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

    source_type = source.get("type", "")
    profile = get_member_profile(group_id, user_id, source_type)
    display_name = (
        (profile or {}).get("displayName")
        or "이름 확인 불가"
    )

    event_id = (
        event.get("webhookEventId")
        or event.get("deliveryContext", {}).get("isRedelivery", False)
        and None
    )

    if not event_id:
        # webhookEventId가 없으면 메시지 ID를 사용합니다.
        event_id = message.get("id")
    if not event_id:
        # 최후의 대체값: 중복 방지가 보장되지 않을 수 있습니다.
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
    start_dt = datetime.combine(start, datetime.min.time(), tzinfo=KST)
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


def gemini_answer(user_text):
    if not GEMINI_API_KEY:
        return (
            "아직 AI 집사 연결이 완료되지 않았어요. "
            "관리자가 Gemini API 설정을 마치면 대답할 수 있어요!"
        )

    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        + GEMINI_MODEL + ":generateContent"
    )

    try:
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
                        "parts": [{"text": user_text[:4000]}],
                    }
                ],
                "generationConfig": {
                    "temperature": 0.8,
                    "maxOutputTokens": 600,
                },
            },
            timeout=25,
        )
        response.raise_for_status()
        data = response.json()
        return data["candidates"][0]["content"]["parts"][0]["text"]
    except Exception:
        logger.exception("Gemini API 요청 실패")
        return (
            "앗, 집사 답변을 가져오지 못했어요. "
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
        rows = get_ranking(group_id, today, today)
        reply_text(
            reply_token,
            format_ranking(rows, today, today),
        )
        return

    date_range = parse_date_range(stripped)
    if date_range:
        start, end = date_range
        rows = get_ranking(group_id, start, end)
        reply_text(
            reply_token,
            format_ranking(rows, start, end),
        )
        return

    if stripped.startswith("집사 "):
        question = stripped[len("집사 "):].strip()
        if question:
            reply_text(reply_token, gemini_answer(question))
        return

    if stripped.startswith("AI "):
        question = stripped[len("AI "):].strip()
        if question:
            reply_text(reply_token, gemini_answer(question))
        return

    if stripped in ("!여초", "!남초"):
        reply_text(
            reply_token,
            "이 명령어의 실제 멤버 검색 및 멘션 기능은 "
            "LINE 그룹 멤버 조회 권한을 확인한 뒤 연결할게요.",
        )
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
            # 텍스트, 스티커, 이미지 등 메시지 이벤트를 기록합니다.
            record_message(event, source, message)

            # 명령어와 AI 질문은 텍스트 메시지만 처리합니다.
            if message.get("type") == "text":
                handle_text(
                    message.get("text", ""),
                    event,
                    source,
                )
        except Exception:
            logger.exception("메시지 이벤트 처리 실패")

    return "OK", 200


# Render의 Gunicorn이 app 객체를 실행합니다.
