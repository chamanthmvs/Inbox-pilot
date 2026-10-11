from typing import Optional

from pydantic import BaseModel


class MessageMeta(BaseModel):
    """
    A single Gmail message as we store it during ingestion — METADATA ONLY.

    We deliberately do NOT keep the message body. Everything here comes from the
    Gmail `format=metadata` response: a few headers, the snippet Gmail already
    exposes, the label ids, and some bookkeeping fields.
    """

    id: str
    thread_id: str

    from_addr: str = ""
    to_addrs: list[str] = []
    subject: str = ""

    # Gmail's own short preview. Not the full body.
    snippet: str = ""

    # Milliseconds since epoch (Gmail `internalDate`), and the raw `Date` header.
    internal_date: Optional[int] = None
    received_at: Optional[str] = None

    label_ids: list[str] = []
    size_estimate: Optional[int] = None

    # The mailbox history id at the time Gmail returned this message. Used to
    # anchor incremental sync.
    history_id: Optional[str] = None
