"""
회의 요약 이메일 발송 (Brevo API)
기존 meeting-summary-fastapi/summarize_meeting.py 의 send_email_notification 그대로 재사용
"""

import os
import re
from typing import Optional
import requests


def send_email_notification(
    to_email: str,
    meeting_title: str,
    meeting_date: str,
    one_liner: str,
    sections: dict,
    page_url: Optional[str] = None,
) -> bool:
    api_key      = os.environ.get("BREVO_API_KEY", "").strip()
    sender_email = os.environ.get("GMAIL_USER", "").strip()

    if not api_key:
        raise ValueError("BREVO_API_KEY 환경변수가 설정되지 않았습니다.")

    recipients = [e.strip() for e in re.split(r"[,\n]", to_email) if e.strip()]
    if not recipients:
        raise ValueError("유효한 이메일 주소가 없습니다.")

    def build_table_html(text: str) -> str:
        rows = [r.strip() for r in text.strip().split("\n") if r.strip()]
        if not rows:
            return ""
        html = (
            '<table border="1" cellpadding="6" cellspacing="0" '
            'style="border-collapse:collapse;width:100%;margin-bottom:12px">'
        )
        for i, row in enumerate(rows):
            cells = row.split("|")
            tag = "th" if i == 0 else "td"
            bg  = "#f0eeff" if i == 0 else ("#fafafa" if i % 2 == 0 else "#fff")
            html += f'<tr style="background:{bg}">'
            html += "".join(
                f'<{tag} style="padding:6px 10px;text-align:left">{c.strip()}</{tag}>'
                for c in cells
            )
            html += "</tr>"
        html += "</table>"
        return html

    section_labels = {
        "개요": "📌 회의 개요",
        "내용": "🗣️ 회의 내용",
        "결정": "✅ 결정 사항",
        "액션": "📋 액션 아이템",
    }
    sections_html = ""
    for key, label in section_labels.items():
        if sections.get(key):
            sections_html += f'<h3 style="color:#6c63ff;margin:18px 0 8px">{label}</h3>'
            sections_html += build_table_html(sections[key])

    notion_btn = (
        f'<a href="{page_url}" style="display:inline-block;margin-top:16px;'
        f'padding:10px 20px;background:#6c63ff;color:#fff;border-radius:8px;'
        f'text-decoration:none;font-weight:600">📝 Notion에서 보기</a>'
    ) if page_url else ""

    html_body = f"""
<div style="font-family:'Segoe UI',sans-serif;max-width:720px;margin:0 auto;padding:24px">
  <div style="background:#f5f4ff;border-radius:12px;padding:20px;margin-bottom:20px">
    <h2 style="margin:0;color:#1a1a2e;font-size:1.2rem">🤖 회의록 요약 완료</h2>
  </div>
  <p style="margin:6px 0"><b>회의명:</b> {meeting_title}</p>
  <p style="margin:6px 0"><b>날짜:</b> {meeting_date}</p>
  <div style="background:#fff8e1;border-left:4px solid #ffd54f;padding:12px 16px;margin:16px 0;border-radius:0 8px 8px 0">
    💡 <b>{one_liner}</b>
  </div>
  {sections_html}
  {notion_btn}
  <p style="color:#aaa;font-size:0.8rem;margin-top:24px">이 메일은 회의록 자동 요약 시스템에서 발송되었습니다.</p>
</div>
"""

    resp = requests.post(
        "https://api.brevo.com/v3/smtp/email",
        headers={"api-key": api_key, "Content-Type": "application/json"},
        json={
            "sender":      {"name": "MeetingOS", "email": sender_email},
            "to":          [{"email": r} for r in recipients],
            "subject":     f"[회의록] {meeting_title} ({meeting_date})",
            "htmlContent": html_body,
        },
        timeout=30,
    )
    if not resp.ok:
        raise RuntimeError(f"Brevo 발송 실패: {resp.status_code} {resp.text}")
    return True
