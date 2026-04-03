"""
회의록 참석자 → 노션 연락처 DB 조회 → 이메일 목록 추출

연락처 DB 컬럼 (API 직접 확인 완료):
  - 이름       : title
  - 노션 계정  : people  ← 참석자 매칭 기준
  - 이메일     : email   ← 추출 대상
"""

import logging
import os
from typing import Dict, List

from notion_client import Client

logger = logging.getLogger(__name__)

CONTACTS_DB_ID = "4e634a54f4814299a5d5c7d2e37653e8"


def _get_attendee_ids(page: dict) -> List[str]:
    """회의록 페이지의 참석자 컬럼에서 Notion user ID 목록 추출."""
    people = page.get("properties", {}).get("참석자", {}).get("people", [])
    return [u["id"] for u in people if u.get("id")]


def _build_contacts_map(notion: Client) -> Dict[str, str]:
    """
    연락처 DB 전체를 읽어 {notion_user_id: email} 매핑 반환.
    한 사람이 여러 노션 계정을 가질 수 있으므로 모두 등록.
    """
    mapping: dict[str, str] = {}
    try:
        resp = notion.databases.query(database_id=CONTACTS_DB_ID)
        rows = resp["results"]
        while resp.get("has_more"):
            resp = notion.databases.query(
                database_id=CONTACTS_DB_ID, start_cursor=resp["next_cursor"]
            )
            rows.extend(resp["results"])
    except Exception as e:
        logger.error(f"연락처 DB 조회 실패: {e}")
        return mapping

    for row in rows:
        props = row["properties"]
        email = props.get("이메일", {}).get("email") or ""
        if not email:
            continue
        for user in props.get("노션 계정", {}).get("people", []):
            uid = user.get("id")
            if uid:
                mapping[uid] = email

    return mapping


def get_attendee_emails(notion: Client, page: dict) -> List[str]:
    """
    회의록 페이지의 참석자를 연락처 DB와 매칭해 이메일 목록 반환.
    매칭 실패 시 빈 리스트 반환 (호출측에서 fallback 처리).
    """
    attendee_ids = _get_attendee_ids(page)
    if not attendee_ids:
        logger.warning("참석자 컬럼이 비어 있습니다.")
        return []

    logger.info(f"참석자 Notion ID: {attendee_ids}")

    contacts = _build_contacts_map(notion)
    emails = []
    for uid in attendee_ids:
        email = contacts.get(uid)
        if email:
            emails.append(email)
            logger.info(f"  매칭: {uid} → {email}")
        else:
            logger.warning(f"  매칭 실패 (연락처 DB에 없음): {uid}")

    return emails
