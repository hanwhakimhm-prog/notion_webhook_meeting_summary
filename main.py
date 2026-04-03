"""
노션 회의 종료 → STT(받아쓰기) 텍스트 → 요약 → 메일 발송 웹훅 서버

보안:
- X-Webhook-Secret 헤더 또는 payload.secret 로 WEBHOOK_SECRET 검증
- 불일치 시 401 반환 + 로그 기록
- User-Agent 보조 체크 (Notion 외 출처 경고)
- /docs, /redoc, /openapi.json 비활성화
"""

import logging
import os
from typing import Optional, Tuple

from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from notion_client import Client

from mailer import send_email_notification
from models import NotionWebhookPayload
from notion_contacts import get_attendee_emails
from summarizer import parse_summary, summarize_with_claude

load_dotenv()

# ── 앱 초기화 ──────────────────────────────────────────────
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

WEBHOOK_SECRET  = os.environ["WEBHOOK_SECRET"]
NOTION_API_KEY  = os.environ["NOTION_API_KEY"]
RECIPIENT_EMAILS = os.environ["RECIPIENT_EMAILS"]  # 콤마 또는 줄바꿈 구분

notion = Client(auth=NOTION_API_KEY)


# ── 유틸 ───────────────────────────────────────────────────
def _extract_page_metadata(page: dict) -> Tuple[str, str, str]:
    """Notion 페이지 객체에서 (회의명, 날짜, url) 추출."""
    props = page.get("properties", {})

    title = "제목없음"
    for key in ["회의명", "이름", "Name", "title"]:
        if key in props:
            title_arr = props[key].get("title", [])
            if title_arr:
                title = title_arr[0].get("plain_text", "제목없음")
                break

    date = ""
    for key in ["회의 날짜", "날짜", "Date"]:
        if key in props:
            date_obj = props[key].get("date")
            if date_obj:
                date = date_obj.get("start", "")
                break

    url = page.get("url", "")
    return title, date, url


def _collect_text_recursive(block_id: str) -> list:
    """블록과 하위 블록에서 텍스트를 재귀적으로 수집."""
    text_block_types = {
        "paragraph", "bulleted_list_item", "numbered_list_item",
        "quote", "toggle", "callout", "heading_1", "heading_2", "heading_3",
    }
    lines = []
    try:
        blocks = notion.blocks.children.list(block_id=block_id)
        results = blocks["results"]
        while blocks.get("has_more"):
            blocks = notion.blocks.children.list(
                block_id=block_id, start_cursor=blocks["next_cursor"]
            )
            results.extend(blocks["results"])
    except Exception:
        return lines

    for b in results:
        btype = b["type"]
        rich_texts = b.get(btype, {}).get("rich_text", [])
        text = "".join(rt.get("plain_text", "") for rt in rich_texts)
        if btype in text_block_types and text.strip():
            lines.append(text)
        if b.get("has_children"):
            lines.extend(_collect_text_recursive(b["id"]))
    return lines


def _extract_stt_text(page_id: str) -> str:
    """
    Notion 페이지 블록에서 받아쓰기(STT) 텍스트 추출.

    우선순위:
      1. transcription 타입 블록 (노션 AI 받아쓰기) → 재귀 추출
      2. '받아쓰기' 제목 블록 이후 텍스트
      3. 페이지 전체 텍스트 (fallback)
    """
    blocks = notion.blocks.children.list(block_id=page_id)
    all_blocks = blocks["results"]
    while blocks.get("has_more"):
        blocks = notion.blocks.children.list(
            block_id=page_id, start_cursor=blocks["next_cursor"]
        )
        all_blocks.extend(blocks["results"])

    # ── 1순위: transcription 블록 ──────────────────────────
    for block in all_blocks:
        if block["type"] == "transcription":
            lines = _collect_text_recursive(block["id"])
            if lines:
                logger.info(f"transcription 블록에서 추출 완료 ({len(lines)}줄)")
                return "\n".join(lines)

    # ── 2순위: '받아쓰기' 제목 이후 텍스트 ────────────────
    text_block_types = {
        "paragraph", "bulleted_list_item", "numbered_list_item",
        "quote", "toggle", "callout",
    }
    heading_types = {"heading_1", "heading_2", "heading_3"}
    stt_lines = []
    in_stt_section = False

    for block in all_blocks:
        btype = block["type"]
        rich_texts = block.get(btype, {}).get("rich_text", [])
        text = "".join(rt.get("plain_text", "") for rt in rich_texts)

        if btype in heading_types:
            if "받아쓰기" in text:
                in_stt_section = True
                continue
            if in_stt_section:
                break
        elif in_stt_section and btype in text_block_types and text.strip():
            stt_lines.append(text)

    if stt_lines:
        logger.info(f"받아쓰기 섹션 추출 완료 ({len(stt_lines)}줄)")
        return "\n".join(stt_lines)

    # ── 3순위: 전체 텍스트 fallback ───────────────────────
    logger.warning("받아쓰기/transcription 섹션 미발견 → 페이지 전체 텍스트 사용")
    all_lines = []
    for block in all_blocks:
        btype = block["type"]
        rich_texts = block.get(btype, {}).get("rich_text", [])
        text = "".join(rt.get("plain_text", "") for rt in rich_texts)
        if text.strip():
            all_lines.append(text)
        if block.get("has_children"):
            all_lines.extend(_collect_text_recursive(block["id"]))
    return "\n".join(all_lines)


# ── 백그라운드 처리 ────────────────────────────────────────
async def _process_meeting(
    page_id: str, page_url: str, title: str, date: str, page: dict
) -> None:
    try:
        logger.info(f"[{title}] 처리 시작")

        stt_text = _extract_stt_text(page_id)
        logger.info(f"[{title}] STT 추출 완료 ({len(stt_text)}자)")

        summary_text = summarize_with_claude(meeting_title=title, memo=stt_text)
        one_liner, sections = parse_summary(summary_text)
        logger.info(f"[{title}] 요약 완료: {one_liner}")

        # ── 수신자 결정: 참석자 → fallback: RECIPIENT_EMAILS ──
        emails = get_attendee_emails(notion, page)
        if emails:
            to_email = ",".join(emails)
            logger.info(f"[{title}] 수신자 (참석자 조회): {to_email}")
        else:
            to_email = RECIPIENT_EMAILS
            logger.warning(f"[{title}] 참석자 매칭 없음 → fallback: {to_email}")

        send_email_notification(
            to_email=to_email,
            meeting_title=title,
            meeting_date=date,
            one_liner=one_liner,
            sections=sections,
            page_url=page_url,
        )
        logger.info(f"[{title}] 이메일 발송 완료 → {to_email}")

    except Exception as e:
        logger.error(f"[{title}] 처리 실패: {e}", exc_info=True)


# ── 웹훅 엔드포인트 ────────────────────────────────────────
@app.post("/webhook/meeting-summary")
async def webhook_meeting_summary(
    request: Request,
    background_tasks: BackgroundTasks,
    x_webhook_secret: Optional[str] = Header(default=None),
) -> JSONResponse:
    """
    노션 버튼 자동화가 POST 하는 웹훅.

    인증 우선순위:
      1. X-Webhook-Secret 헤더
      2. payload body 의 "secret" 필드
    """
    user_agent = request.headers.get("user-agent", "")

    # ── 1. Body 파싱 ──────────────────────────────────────
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="JSON 파싱 실패")

    # ── 2. Secret 검증 ────────────────────────────────────
    payload_secret = body.get("secret") if isinstance(body, dict) else None
    received_secret = x_webhook_secret or payload_secret

    if received_secret != WEBHOOK_SECRET:
        logger.warning(
            f"인증 실패 | IP={request.client.host} | UA={user_agent}"
        )
        raise HTTPException(status_code=401, detail="Unauthorized")

    # ── 3. User-Agent 보조 체크 ───────────────────────────
    ua_lower = user_agent.lower()
    if user_agent and "notion" not in ua_lower and "python" not in ua_lower:
        logger.warning(f"비표준 User-Agent 감지: {user_agent}")

    # ── 4. page_id 추출 ───────────────────────────────────
    # 노션 자동화 payload: {"data": {"id": "<page_id>", ...}, "source": {...}}
    # 직접 호출 fallback:  {"page_id": "<page_id>", "secret": "..."}
    data = body.get("data") or {}
    page_id = data.get("id") or body.get("page_id")
    if not page_id:
        raise HTTPException(status_code=400, detail="page_id를 찾을 수 없습니다.")

    # ── 5. Notion 페이지 메타 조회 ────────────────────────
    try:
        page = notion.pages.retrieve(page_id=page_id)
    except Exception as e:
        logger.error(f"Notion 페이지 조회 실패: {e}")
        raise HTTPException(status_code=500, detail="Notion 페이지 조회 실패")

    title, date, page_url = _extract_page_metadata(page)
    logger.info(f"웹훅 수신: {title} ({date}) page_id={page_id}")

    # ── 6. 백그라운드 처리 위임 ───────────────────────────
    background_tasks.add_task(_process_meeting, page_id, page_url, title, date, page)

    return JSONResponse({"status": "accepted", "title": title}, status_code=202)


# ── 헬스체크 ───────────────────────────────────────────────
@app.get("/health")
async def health() -> JSONResponse:
    return JSONResponse({"status": "ok"})
