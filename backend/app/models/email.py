from typing import Optional

from pydantic import BaseModel


class Attachment(BaseModel):
    filename: str
    mime_type: str
    attachment_id: Optional[str] = None


class Email(BaseModel):
    id: str
    thread_id: str

    sender: str
    recipients: list[str]

    subject: str
    body: str

    received_at: Optional[str] = None

    labels: list[str] = []

    attachments: list[Attachment] = []