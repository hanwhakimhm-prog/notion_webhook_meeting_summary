"""
로컬 테스트 스크립트 - 서버 없이 직접 실행
노션 페이지에서 STT 텍스트 추출 → Claude 요약 → 결과 출력
"""

import os
import re
import sys

# 로컬 .env 우선, 없는 키는 meeting-summary-fastapi .env에서 보충
from dotenv import load_dotenv
load_dotenv(dotenv_path=os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
load_dotenv(
    dotenv_path=os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "..", "meeting-summary-fastapi", ".env"
    )
)

from notion_client import Client
import anthropic
import requests as req_lib

NOTION_API_KEY   = os.environ["NOTION_API_KEY"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]

notion = Client(auth=NOTION_API_KEY)
claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

PAGE_ID = "33735e77c23d805a9170f9a806fc5bdd"


# ── 1. 페이지 메타 조회 ────────────────────────────────────
def get_page_meta(page_id):
    page = notion.pages.retrieve(page_id=page_id)
    props = page.get("properties", {})
    title = "제목없음"
    for key in ["회의명", "이름", "Name", "title"]:
        if key in props:
            arr = props[key].get("title", [])
            if arr:
                title = arr[0].get("plain_text", "제목없음")
                break
    date = ""
    for key in ["회의 날짜", "날짜", "Date"]:
        if key in props:
            d = props[key].get("date")
            if d:
                date = d.get("start", "")
                break
    url = page.get("url", "")
    return title, date, url


# ── 2. 재귀 텍스트 수집 ────────────────────────────────────
TEXT_BLOCK_TYPES = {
    "paragraph", "bulleted_list_item", "numbered_list_item",
    "quote", "toggle", "callout", "heading_1", "heading_2", "heading_3",
}

def collect_text_recursive(block_id, depth=0):
    lines = []
    try:
        resp = notion.blocks.children.list(block_id=block_id)
        results = resp["results"]
        while resp.get("has_more"):
            resp = notion.blocks.children.list(block_id=block_id, start_cursor=resp["next_cursor"])
            results.extend(resp["results"])
    except Exception:
        return lines

    for b in results:
        btype = b["type"]
        rich = b.get(btype, {}).get("rich_text", [])
        text = "".join(rt.get("plain_text", "") for rt in rich)
        indent = "  " * depth
        if btype in TEXT_BLOCK_TYPES and text.strip():
            print(f"    {indent}[{btype}] {text[:80]}")
            lines.append(text)
        if b.get("has_children"):
            lines.extend(collect_text_recursive(b["id"], depth + 1))
    return lines


def extract_stt_text(page_id):
    blocks = notion.blocks.children.list(block_id=page_id)
    all_blocks = blocks["results"]
    while blocks.get("has_more"):
        blocks = notion.blocks.children.list(block_id=page_id, start_cursor=blocks["next_cursor"])
        all_blocks.extend(blocks["results"])

    print(f"\n  📦 최상위 블록 수: {len(all_blocks)}")

    # 1순위: transcription 블록
    for b in all_blocks:
        if b["type"] == "transcription":
            print("  ✅ transcription 블록 발견 → 재귀 추출 시작")
            lines = collect_text_recursive(b["id"])
            if lines:
                return "\n".join(lines)

    # 2순위: '받아쓰기' 제목 이후 텍스트
    heading_types = {"heading_1", "heading_2", "heading_3"}
    stt_lines = []
    in_stt = False
    for b in all_blocks:
        btype = b["type"]
        rich = b.get(btype, {}).get("rich_text", [])
        text = "".join(rt.get("plain_text", "") for rt in rich)
        if btype in heading_types:
            if "받아쓰기" in text:
                in_stt = True
                print(f"  ✅ 받아쓰기 섹션 발견: [{text}]")
                continue
            if in_stt:
                break
        elif in_stt and btype in TEXT_BLOCK_TYPES and text.strip():
            stt_lines.append(text)
    if stt_lines:
        return "\n".join(stt_lines)

    # 3순위: 전체 텍스트 fallback
    print("  ⚠️  받아쓰기/transcription 없음 → 전체 텍스트 fallback")
    all_lines = []
    for b in all_blocks:
        btype = b["type"]
        rich = b.get(btype, {}).get("rich_text", [])
        text = "".join(rt.get("plain_text", "") for rt in rich)
        if text.strip():
            all_lines.append(text)
        if b.get("has_children"):
            all_lines.extend(collect_text_recursive(b["id"]))
    return "\n".join(all_lines)


# ── 3. Claude 요약 ─────────────────────────────────────────
def summarize(title, stt_text):
    prompt = f"""회의명: {title}

아래 입력 자료를 바탕으로 회의 내용을 요약해주세요.
입력 자료 중 [회의 중 메모]는 가장 중요한 정보입니다. 반드시 최우선으로 반영하고, 메모에 언급된 내용은 빠짐없이 결과에 포함시켜주세요.

[회의 중 메모] ← 최우선 반영
{stt_text or "(없음)"}

위 내용을 아래 형식에 맞게 정리해주세요.
마크다운 문법(**굵게**, -, ---, ## 등)은 절대 사용하지 마세요.
구분자로 [SECTION] 태그만 사용해주세요.

[SECTION:개요]
회의명 | {title}
회의 날짜 | (yyyy-mm-dd(요일) 형식)
목적 | (회의 목적 한 줄)
배경 | (배경 한 줄)
참석자 | (참석자)

[SECTION:내용]
주제 | 내용
(주요 논의 주제) | (내용 요약)
...

[SECTION:결정]
번호 | 결정 사항
1 | (결정 내용)
...

[SECTION:액션]
담당자 | 할 일 | 기한
(담당자) | (할 일) | (기한)
...

[SECTION:한줄요약]
(전체 회의를 한 문장으로)
"""
    resp = claude.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=4096,
        messages=[{"role": "user", "content": prompt}],
    )
    return resp.content[0].text


# ── 4. 파싱 ───────────────────────────────────────────────
def parse(summary_text):
    sections = {}
    for key, content in re.findall(
        r"\[SECTION:(\w+)\](.*?)(?=\[SECTION:|$)", summary_text, re.DOTALL
    ):
        sections[key] = content.strip()
    return sections.get("한줄요약", "").strip(), sections


# ── 메인 ──────────────────────────────────────────────────
if __name__ == "__main__":
    print(f"🔍 페이지 조회 중... {PAGE_ID}")
    title, date, url = get_page_meta(PAGE_ID)
    print(f"  📄 회의명: {title}")
    print(f"  📅 날짜:   {date}")
    print(f"  🔗 URL:    {url}")

    print("\n📝 STT 텍스트 추출 중...")
    stt = extract_stt_text(PAGE_ID)
    print(f"\n  STT 텍스트 ({len(stt)}자):\n{'─'*50}")
    print(stt[:500] + ("..." if len(stt) > 500 else ""))
    print("─"*50)

    if not stt.strip():
        print("\n❌ 텍스트가 없습니다. 노션 페이지에 내용을 확인해주세요.")
        sys.exit(1)

    print("\n🤖 Claude 요약 중...")
    summary = summarize(title, stt)

    one_liner, sections = parse(summary)

    print(f"\n{'='*60}")
    print(f"💡 한줄 요약: {one_liner}")
    print(f"{'='*60}")
    for key, content in sections.items():
        if key != "한줄요약":
            label = {"개요":"📌 개요","내용":"🗣️ 내용","결정":"✅ 결정","액션":"📋 액션"}.get(key, key)
            print(f"\n{label}\n{content}")
    print(f"\n{'='*60}")

    # ── 이메일 발송 ────────────────────────────────────────
    brevo_key    = os.environ.get("BREVO_API_KEY", "").strip()
    sender_email = os.environ.get("GMAIL_USER", "").strip()
    recipients   = os.environ.get("RECIPIENT_EMAILS", "").strip()

    if not brevo_key:
        print("⚠️  BREVO_API_KEY 없음 → 이메일 생략")
        sys.exit(0)
    if not recipients:
        print("⚠️  RECIPIENT_EMAILS 없음 → 이메일 생략")
        sys.exit(0)

    print(f"\n📧 이메일 발송 중 → {recipients}")

    def build_table_html(text):
        rows = [r.strip() for r in text.strip().split("\n") if r.strip()]
        if not rows:
            return ""
        html = '<table border="1" cellpadding="6" cellspacing="0" style="border-collapse:collapse;width:100%;margin-bottom:12px">'
        for i, row in enumerate(rows):
            cells = row.split("|")
            tag = "th" if i == 0 else "td"
            bg  = "#f0eeff" if i == 0 else ("#fafafa" if i % 2 == 0 else "#fff")
            html += f'<tr style="background:{bg}">'
            html += "".join(f'<{tag} style="padding:6px 10px;text-align:left">{c.strip()}</{tag}>' for c in cells)
            html += "</tr>"
        html += "</table>"
        return html

    section_labels = {"개요":"📌 회의 개요","내용":"🗣️ 회의 내용","결정":"✅ 결정 사항","액션":"📋 액션 아이템"}
    sections_html = ""
    for key, label in section_labels.items():
        if sections.get(key):
            sections_html += f'<h3 style="color:#6c63ff;margin:18px 0 8px">{label}</h3>'
            sections_html += build_table_html(sections[key])

    notion_btn = (f'<a href="{url}" style="display:inline-block;margin-top:16px;'
                  f'padding:10px 20px;background:#6c63ff;color:#fff;border-radius:8px;'
                  f'text-decoration:none;font-weight:600">📝 Notion에서 보기</a>') if url else ""

    html_body = f"""
<div style="font-family:'Segoe UI',sans-serif;max-width:720px;margin:0 auto;padding:24px">
  <div style="background:#f5f4ff;border-radius:12px;padding:20px;margin-bottom:20px">
    <h2 style="margin:0;color:#1a1a2e;font-size:1.2rem">🤖 회의록 요약 완료</h2>
  </div>
  <p style="margin:6px 0"><b>회의명:</b> {title}</p>
  <p style="margin:6px 0"><b>날짜:</b> {date or "미입력"}</p>
  <div style="background:#fff8e1;border-left:4px solid #ffd54f;padding:12px 16px;margin:16px 0;border-radius:0 8px 8px 0">
    💡 <b>{one_liner}</b>
  </div>
  {sections_html}
  {notion_btn}
  <p style="color:#aaa;font-size:0.8rem;margin-top:24px">이 메일은 회의록 자동 요약 시스템에서 발송되었습니다.</p>
</div>"""

    to_list = [{"email": e.strip()} for e in re.split(r"[,\n]", recipients) if e.strip()]
    resp = req_lib.post(
        "https://api.brevo.com/v3/smtp/email",
        headers={"api-key": brevo_key, "Content-Type": "application/json"},
        json={
            "sender":      {"name": "MeetingOS", "email": sender_email},
            "to":          to_list,
            "subject":     f"[회의록] {title} ({date or '날짜미입력'})",
            "htmlContent": html_body,
        },
        timeout=30,
    )

    if resp.ok:
        print(f"✅ 이메일 발송 완료!")
    else:
        print(f"❌ 이메일 발송 실패: {resp.status_code} {resp.text}")
