"""
회의 요약 결과를 Notion 페이지 하단에 추가.
기존 meeting-summary-fastapi/summarize_meeting.py 의 update_notion_page 그대로 재사용.
"""

import logging
from notion_client import Client

logger = logging.getLogger(__name__)

SUMMARY_MARKER = "🤖 AI 요약 |"


def _rich(text: str, color: str = None) -> list:
    obj = {"type": "text", "text": {"content": str(text)[:2000]}}
    if color:
        obj["annotations"] = {"color": color}
    return [obj]


def _make_table_rows(rows_text: str, col_count: int) -> list:
    rows = []
    for line in rows_text.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        cells = [c.strip() for c in line.split("|")]
        while len(cells) < col_count:
            cells.append("")
        cells = cells[:col_count]
        rows.append({
            "object": "block", "type": "table_row",
            "table_row": {"cells": [_rich(c) for c in cells]},
        })
    return rows


def _delete_existing_summary(notion: Client, page_id: str) -> None:
    """기존 AI 요약 블록이 있으면 삭제 (중복 방지)."""
    blocks = notion.blocks.children.list(block_id=page_id)
    all_blocks = blocks["results"]
    while blocks.get("has_more"):
        blocks = notion.blocks.children.list(block_id=page_id, start_cursor=blocks["next_cursor"])
        all_blocks.extend(blocks["results"])

    marker_idx = None
    for i, block in enumerate(all_blocks):
        if block["type"] == "callout":
            text = "".join(
                rt.get("plain_text", "")
                for rt in block.get("callout", {}).get("rich_text", [])
            )
            if SUMMARY_MARKER in text:
                marker_idx = i
                break

    if marker_idx is None:
        return

    start_idx = marker_idx
    if marker_idx > 0 and all_blocks[marker_idx - 1]["type"] == "divider":
        start_idx = marker_idx - 1

    for block in all_blocks[start_idx:]:
        try:
            notion.blocks.delete(block_id=block["id"])
        except Exception as e:
            logger.warning(f"블록 삭제 실패 ({block['id']}): {e}")

    logger.info("기존 요약 블록 삭제 완료")


def append_summary_to_page(
    notion: Client,
    page_id: str,
    meeting_title: str,
    meeting_date: str,
    one_liner: str,
    sections: dict,
) -> None:
    """요약 결과를 페이지 하단에 추가 (기존 요약이 있으면 먼저 삭제)."""
    _delete_existing_summary(notion, page_id)

    new_blocks = [
        {"object": "block", "type": "divider", "divider": {}},
        {
            "object": "block", "type": "callout",
            "callout": {
                "rich_text": _rich(f"{SUMMARY_MARKER} {meeting_date}\n{one_liner}"),
                "icon": {"type": "emoji", "emoji": "🤖"},
                "color": "blue_background",
            },
        },
    ]

    if sections.get("개요"):
        new_blocks.append({
            "object": "block", "type": "heading_2",
            "heading_2": {"rich_text": _rich("📌 회의 개요")},
        })
        rows = _make_table_rows(sections["개요"], 2)
        if rows:
            new_blocks.append({
                "object": "block", "type": "table",
                "table": {"table_width": 2, "has_column_header": False,
                           "has_row_header": True, "children": rows},
            })

    if sections.get("내용"):
        new_blocks.append({
            "object": "block", "type": "heading_2",
            "heading_2": {"rich_text": _rich("🗣️ 회의 내용")},
        })
        rows = _make_table_rows(sections["내용"], 2)
        if rows:
            new_blocks.append({
                "object": "block", "type": "table",
                "table": {"table_width": 2, "has_column_header": True,
                           "has_row_header": False, "children": rows},
            })

    if sections.get("결정"):
        new_blocks.append({
            "object": "block", "type": "heading_2",
            "heading_2": {"rich_text": _rich("✅ 결정 사항")},
        })
        rows = _make_table_rows(sections["결정"], 2)
        if rows:
            new_blocks.append({
                "object": "block", "type": "table",
                "table": {"table_width": 2, "has_column_header": True,
                           "has_row_header": False, "children": rows},
            })

    if sections.get("액션"):
        new_blocks.append({
            "object": "block", "type": "heading_2",
            "heading_2": {"rich_text": _rich("📋 액션 아이템")},
        })
        rows = _make_table_rows(sections["액션"], 3)
        if rows:
            new_blocks.append({
                "object": "block", "type": "table",
                "table": {"table_width": 3, "has_column_header": True,
                           "has_row_header": False, "children": rows},
            })

    new_blocks.append({"object": "block", "type": "divider", "divider": {}})

    # Notion API 한 번에 최대 100블록 제한 → 90개씩 나눠서 append
    for i in range(0, len(new_blocks), 90):
        notion.blocks.children.append(block_id=page_id, children=new_blocks[i:i+90])

    logger.info(f"Notion 페이지 요약 추가 완료 ({len(new_blocks)}블록)")
