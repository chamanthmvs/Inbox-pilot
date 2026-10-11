import base64

from app.models.email import Email, Attachment
from app.models.message import MessageMeta


def _get_headers(payload: dict) -> dict:
    headers = {}

    for header in payload.get("headers", []):
        name = header["name"].lower()
        value = header["value"]

        headers[name] = value

    return headers


def _decode_body(data: str) -> str:
    if not data:
        return ""

    decoded = base64.urlsafe_b64decode(data)

    return decoded.decode("utf-8", errors="replace")


def _extract_content(part: dict, body_parts: list[str], attachments: list[Attachment]):
    """
    Recursively walk through a Gmail MIME part.

    Collect:
        - text/plain content
        - attachment metadata
    """

    mime_type = part.get("mimeType", "")
    filename = part.get("filename", "")
    part_body = part.get("body", {})

    body_data = part_body.get("data")
    attachment_id = part_body.get("attachmentId")

    # ---------------------------------------
    # Case 1: Plain text email content
    # ---------------------------------------

    if mime_type == "text/plain" and body_data:
        body_parts.append(
            _decode_body(body_data)
        )

    # ---------------------------------------
    # Case 2: Attachment
    # ---------------------------------------

    elif filename:

        attachments.append(
            Attachment(
                filename=filename,
                mime_type=mime_type,
                attachment_id=attachment_id,
            )
        )

    # ---------------------------------------
    # Case 3: Nested MIME parts
    # ---------------------------------------

    for child_part in part.get("parts", []):

        _extract_content(
            child_part,
            body_parts,
            attachments
        )


def parse_email(raw_message: dict) -> Email:

    payload = raw_message["payload"]

    headers = _get_headers(payload)

    body_parts = []
    attachments = []

    # Start walking from the root payload
    _extract_content(
        payload,
        body_parts,
        attachments
    )

    # Combine all text/plain pieces
    body = "\n".join(body_parts)

    # Extract recipients
    recipients = []

    if headers.get("to"):
        recipients = [
            email.strip()
            for email in headers["to"].split(",")
        ]

    return Email(
        id=raw_message["id"],
        thread_id=raw_message["threadId"],
        sender=headers.get("from", ""),
        recipients=recipients,
        subject=headers.get("subject", ""),
        body=body,
        received_at=headers.get("date"),
        labels=raw_message.get("labelIds", []),
        attachments=attachments,
    )


def parse_metadata(raw_message: dict) -> MessageMeta:
    """
    Parse a Gmail `format=metadata` response into a MessageMeta.

    This is the ingestion path: it reads only headers + snippet + labels and
    never touches the message body (metadata responses don't include it anyway).
    Works equally on a full message (handy for tests with a captured fixture).
    """

    payload = raw_message.get("payload", {})
    headers = _get_headers(payload)

    recipients = []
    if headers.get("to"):
        recipients = [
            addr.strip()
            for addr in headers["to"].split(",")
            if addr.strip()
        ]

    internal_date = raw_message.get("internalDate")
    if internal_date is not None:
        internal_date = int(internal_date)

    size_estimate = raw_message.get("sizeEstimate")
    history_id = raw_message.get("historyId")

    return MessageMeta(
        id=raw_message["id"],
        thread_id=raw_message["threadId"],
        from_addr=headers.get("from", ""),
        to_addrs=recipients,
        subject=headers.get("subject", ""),
        snippet=raw_message.get("snippet", ""),
        internal_date=internal_date,
        received_at=headers.get("date"),
        label_ids=raw_message.get("labelIds", []),
        size_estimate=size_estimate,
        history_id=str(history_id) if history_id is not None else None,
    )