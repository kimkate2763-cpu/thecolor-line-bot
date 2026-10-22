
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
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY", "").strip()

SYSTEM_PROMPT = """
너는 LINE 단체방 '더컬러'의 친근하고 다정한 AI 집사야.
자연스러운 한국어 존댓말을 사용해.
답변은 보통 1~4문장으로 간결하게 작성해.
사용자의 감정을 먼저 이해하고, 필요할 때 실질적인 도움을 줘.
확인하지 못한 방 규칙이나 사실을 지어내지 마.
검색 자료가 제공되면 그 자료를 우선 참고해 답변해.
자료에 없는 내용은 확인된 사실처럼 단정하지 마.
검색 자료의 URL을 답변에 그대로 길게 나열하지 말고,
필요한 출처는 답변 끝에 간결하게 표시해.
"""

# 현재 공식적으로 제공되는 모델 ID를 우선 사용합니다.
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
    """최신 정보가 필요한 질문에 한해 Tavily 검색을 실행합니다."""
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
            timeout=15,
        )

        if not response.ok:
            logger.warning(
                "Tavily 검색 실패: HTTP %s %s",
                response.status_code,
                response.text[:400],
            )
            return []

        data = response.json()
        results = data.get("results", [])

        cleaned = []
        for item in results[:5]:
            title = str(item.get("title", "")).strip()
            url = str(item.get("url", "")).strip()
            content = str(item.get("content", "")).strip()

            if not content:
                continue

            cleaned.append({
                "title": title[:200],
                "url": url,
                "content": content[:1800],
            })

        logger.info("Tavily 검색 성공: 결과 %s개", len(cleaned))
        return cleaned

    except requests.exceptions.RequestException:
        logger.exception("Tavily 통신 오류")
        return []
    except (ValueError, TypeError):
        logger.exception("Tavily 응답 처리 오류")
        return []


def build_search_context(results):
    if not results:
        return "", []

    context_lines = [
        "다음은 웹 검색 결과입니다.",
        "검색 결과가 질문과 관련 있는지 판단한 뒤 사용하세요.",
    ]
    sources = []

    for index, item in enumerate(results, start=1):
        context_lines.append(
            f"\n[{index}] {item['title']}\n{item['content']}"
        )

        url = item["url"]
        if url.startswith("https://") or url.startswith("http://"):
            sources.append((item["title"] or "참고 자료", url))

    return "\n".join(context_lines), sources


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


def record_message(event, source, message):
    group_id = source.get("groupId") or source.get("roomId")
    user_id = source.get("userId")

    if not group_id or not user_id:
        return

    ensure_database()

    profile = get_member_profile(
        group_id,
        user_id,
        source.get("type", ""),
    )

    display_name = (
        (profile or {}).get("displayName") or "이름 확인 불가"
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


def gemini_answer(user_text):
    if not GEMINI_API_KEY:
        logger.error("GEMINI_API_KEY가 설정되지 않았습니다.")
        return (
            "아직 AI 집사 연결이 완료되지 않았어요. "
            "관리자가 Gemini API 키 설정을 확인해 주세요."
        )

    # 최신 정보가 필요한 질문만 외부 검색합니다.
    search_results = []
    if should_search_web(user_text):
        search_results = tavily_search(user_text)

    search_context, sources = build_search_context(search_results)

    prompt = user_text[:3500]
    if search_context:
        prompt += (
            "\n\n[웹 검색 참고 자료]\n"
            + search_context[:8000]
            + "\n\n검색 자료를 근거로 답변하고, 자료에 없는 내용은 "
              "확실하다고 단정하지 마세요."
        )
    elif should_search_web(user_text):
        prompt += (
            "\n\n웹 검색 결과를 가져오지 못했습니다. "
            "최신 정보를 확인한 것처럼 말하지 말고, "
            "확인할 수 없다고 솔직하게 알려 주세요."
        )

    last_error = None

    for model in get_gemini_models():
        url = (
            "https://generativelanguage.googleapis.com/"
            "v1beta/models/" + model + ":generateContent"
        )

        try:
            logger.info("Gemini 모델 시도: %s", model)

            response = requests.post(
                url,
                headers={
                    "x-goog-api-key": GEMINI_API_KEY,
                    "Content-Type": "application/json",
                },
                json={
                    "systemInstruction": {
                        "parts": [{"text": SYSTEM_PROMPT}]
                    },
                    "contents": [{
                        "role": "user",
                        "parts": [{"text": prompt}]
                    }],
                    "generationConfig": {
                        "maxOutputTokens": 600
                    },
                },
                timeout=40,
            )

            if not response.ok:
                last_error = (
                    f"{model}: HTTP {response.status_code} "
                    f"{response.text[:700]}"
                )
                logger.warning("Gemini 모델 실패: %s", last_error)

                # 429는 사용량/비율 제한일 수 있습니다.
                # 같은 요청을 여러 모델로 반복하지 않습니다.
                if response.status_code == 429:
                    logger.error(
                        "Gemini 429 한도 오류. 모델 반복 시도를 중단합니다."
                    )
                    break

                # 모델 ID가 잘못됐거나 접근할 수 없는 경우는
                # 다음 모델을 시도할 수 있습니다.
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
                last_error = (
                    f"{model}: 답변 텍스트 없음. "
                    f"finishReason={candidate.get('finishReason', 'unknown')}"
                )
                logger.warning("%s", last_error)
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

    if last_error and "HTTP 429" in last_error:
        return (
            "집사가 지금 AI 사용 한도에 걸렸어요. 😿\n"
            "잠시 후 다시 시도해 주세요. 계속되면 관리자가 "
            "Google AI Studio의 모델별 한도를 확인해야 해요."
        )

    return (
        "집사가 답변을 가져오지 못했어요. 😿\n"
        "관리자가 Render 로그에서 오류를 확인해야 해요."
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
        reply_text(reply_token, gemini_answer(question))
        return

    # 일반 채팅에는 답변하지 않습니다.


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
