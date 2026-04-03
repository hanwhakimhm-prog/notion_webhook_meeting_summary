"""
STT 텍스트 → Claude API 회의 요약
기존 meeting-summary-fastapi/summarize_meeting.py 프롬프트·파싱 포맷 그대로 재사용
"""

import os
import re
from typing import Optional, Tuple
import anthropic

_client: Optional[anthropic.Anthropic] = None


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    return _client


def summarize_with_claude(
    meeting_title: str,
    memo: str,
    transcript: Optional[str] = None,
    attached_text: str = "",
    agenda_text: str = "",
) -> str:
    """
    STT 텍스트(memo) + 선택적 transcript 를 받아 Claude 로 회의 요약을 생성.
    - memo: 받아쓰기(STT) 텍스트 (최우선 반영)
    - transcript: 별도 녹취록이 있을 경우 (보조)
    """
    transcript_section = f"\n\n[녹음 전문]\n{transcript}" if transcript else ""
    attached_section   = f"\n\n[첨부 파일 내용]\n{attached_text}" if attached_text else ""
    agenda_section     = f"\n\n[사전 안건]\n{agenda_text}" if agenda_text else ""

    prompt = f"""회의명: {meeting_title}

아래 입력 자료를 바탕으로 회의 내용을 요약해주세요.
입력 자료 중 [회의 중 메모]는 가장 중요한 정보입니다. 반드시 최우선으로 반영하고, 메모에 언급된 내용은 빠짐없이 결과에 포함시켜주세요.
STT 녹취록과 첨부 파일은 메모를 보완하는 참고 자료로 활용해주세요.
사전 안건이 있을 경우 회의 흐름 파악에만 참고하고, 실제 결과는 메모와 녹취록 기준으로 작성해주세요.

[회의 중 메모] ← 최우선 반영
{memo or "(없음)"}
{transcript_section}
{attached_section}
{agenda_section}

위 내용을 아래 형식에 맞게 정리해주세요.
마크다운 문법(**굵게**, -, ---, ## 등)은 절대 사용하지 마세요.
구분자로 [SECTION] 태그만 사용해주세요.

[SECTION:개요]
회의명 | {meeting_title}
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

    response = _get_client().messages.create(
        model="claude-sonnet-4-6",
        max_tokens=4096,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text


def parse_summary(summary_text: str) -> Tuple[str, dict]:
    """[SECTION:key] 태그로 분리된 요약 텍스트를 파싱."""
    sections: dict[str, str] = {}
    pattern = r"\[SECTION:(\w+)\](.*?)(?=\[SECTION:|$)"
    for key, content in re.findall(pattern, summary_text, re.DOTALL):
        sections[key] = content.strip()
    one_liner = sections.get("한줄요약", "").strip()
    return one_liner, sections
