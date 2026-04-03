from pydantic import BaseModel
from typing import Optional


class NotionWebhookSource(BaseModel):
    type: str
    automation_id: Optional[str] = None
    event_id: Optional[str] = None

    class Config:
        extra = "allow"


class NotionWebhookData(BaseModel):
    object: Optional[str] = None
    id: Optional[str] = None
    url: Optional[str] = None

    class Config:
        extra = "allow"


class NotionWebhookPayload(BaseModel):
    """노션 자동화 버튼 웹훅 payload 구조"""
    source: Optional[NotionWebhookSource] = None
    data: Optional[NotionWebhookData] = None
    # 직접 전달 시 추가 필드 허용
    secret: Optional[str] = None
    page_id: Optional[str] = None

    class Config:
        extra = "allow"
