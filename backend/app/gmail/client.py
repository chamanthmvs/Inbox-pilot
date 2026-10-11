from pathlib import Path

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
import random
import time

from googleapiclient.errors import HttpError


# gmail.modify: read messages, create labels, and apply/remove labels on
# messages. It does NOT allow permanent deletion. This single scope covers all
# of v1 (label + categorize). Changing this list invalidates an existing
# token.json — the user must delete it and re-consent once.
SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify"
]


class GmailClient:

    def __init__(self):
        self.service = self._authenticate()

    def _authenticate(self):

        creds = None

        token_path = Path("token.json")
        credentials_path = Path("credentials.json")

        # Try to load an existing OAuth token
        if token_path.exists():
            creds = Credentials.from_authorized_user_file(
                token_path,
                SCOPES
            )

        # We don't have a valid token
        if not creds or not creds.valid:

            refreshed = False

            # Token exists but has expired — try a silent refresh first.
            if creds and creds.expired and creds.refresh_token:
                try:
                    creds.refresh(Request())
                    refreshed = True
                except RefreshError:
                    # Revoked, expired beyond refresh, or scope changed →
                    # the old token is useless. Drop it and re-consent.
                    refreshed = False
                    if token_path.exists():
                        token_path.unlink()
                    creds = None

            # First-time auth, or recovery from a dead token: full consent flow.
            if not refreshed:
                flow = InstalledAppFlow.from_client_secrets_file(
                    credentials_path,
                    SCOPES
                )

                creds = flow.run_local_server(
                    port=0
                )

            # Save the OAuth credentials
            token_path.write_text(
                creds.to_json()
            )

        # Create Gmail API service
        return build(
            "gmail",
            "v1",
            credentials=creds
        )

    def get_profile(self):
        request = self.service.users().getProfile(userId="me")
        return self._execute_with_retry(request)

    def list_messages(self, max_results=100, page_token=None):
        request = (
            self.service
            .users()
            .messages()
            .list(
                userId="me",
                maxResults=max_results,
                pageToken=page_token,
            )
        )
        response = self._execute_with_retry(request)
        return (
            response.get("messages", []),
            response.get("nextPageToken"),
        )

    def _execute_with_retry(self, request, max_retries=8):
        """
        Execute a Gmail request, retrying transient failures with exponential
        backoff: rate limits (403 rateLimit/userRateLimit, 429) and server
        errors (5xx). Non-transient errors (400/401/404) raise immediately.
        Tuned for long full-inbox crawls.
        """

        for attempt in range(max_retries):

            try:
                return request.execute()

            except HttpError as error:
                status = error.resp.status
                text = (
                    error.content.decode("utf-8", errors="ignore")
                    if error.content
                    else ""
                )

                transient = status in (429, 500, 502, 503, 504) or (
                    status == 403
                    and ("rateLimitExceeded" in text or "userRateLimitExceeded" in text)
                )

                if not transient or attempt == max_retries - 1:
                    raise

                # Exponential backoff, capped, with jitter.
                wait_time = min(60.0, 2 ** attempt) + random.random()
                print(
                    f"Transient Gmail error {status}; "
                    f"retry {attempt + 1}/{max_retries} in {wait_time:.1f}s..."
                )
                time.sleep(wait_time)
    
    def get_message(self, message_id):

        request = (
        self.service
        .users()
        .messages()
        .get(
            userId="me",
            id=message_id,
            format="full"
        )
    )

        return self._execute_with_retry(request)

    def get_message_metadata(
        self,
        message_id,
        headers=("From", "To", "Subject", "Date"),
    ):
        """
        Fetch a single message as METADATA ONLY — the given headers plus the
        snippet and label ids. No body is returned. This is the ingestion path
        (data minimization); it still costs the same quota units as a full get.
        """

        request = (
            self.service
            .users()
            .messages()
            .get(
                userId="me",
                id=message_id,
                format="metadata",
                metadataHeaders=list(headers),
            )
        )

        return self._execute_with_retry(request)

    def get_messages_metadata_batch(
        self,
        message_ids,
        headers=("From", "To", "Subject", "Date"),
        batch_size=50,
        max_retries=6,
    ):
        """
        Fetch metadata for many messages using Gmail BATCH requests — up to
        `batch_size` messages per HTTP round-trip. Returns {id: raw | None}.

        Transient per-item failures (429/5xx/rate-limit) are retried with
        backoff; a whole-batch transient error retries the batch. Non-transient
        failures yield None for that id (the crawl skips it and continues).
        """
        results = {}
        ids = list(message_ids)
        if not ids:
            return results

        def _make(mid):
            return (
                self.service.users().messages().get(
                    userId="me", id=mid, format="metadata", metadataHeaders=list(headers)
                )
            )

        def _transient(status):
            return status in (403, 429, 500, 502, 503, 504)

        for start in range(0, len(ids), batch_size):
            remaining = ids[start : start + batch_size]

            for attempt in range(max_retries):
                if not remaining:
                    break
                failed = {}

                def _cb(request_id, response, exception, _failed=failed):
                    if exception is not None:
                        status = getattr(getattr(exception, "resp", None), "status", None)
                        _failed[request_id] = status
                    else:
                        results[request_id] = response

                batch = self.service.new_batch_http_request(callback=_cb)
                for mid in remaining:
                    batch.add(_make(mid), request_id=mid)

                try:
                    batch.execute()
                except HttpError as error:
                    if _transient(error.resp.status) and attempt < max_retries - 1:
                        time.sleep(min(60.0, 2 ** attempt) + random.random())
                        continue
                    raise

                retry = [mid for mid, status in failed.items() if _transient(status)]
                for mid, status in failed.items():
                    if not _transient(status):
                        results.setdefault(mid, None)  # give up on this one

                remaining = retry
                if remaining and attempt < max_retries - 1:
                    time.sleep(min(60.0, 2 ** attempt) + random.random())

            for mid in remaining:  # exhausted retries
                results.setdefault(mid, None)

        return results

    def list_history(self, start_history_id, page_token=None):
        """
        List mailbox changes since `start_history_id` (incremental sync).

        Raises HttpError 404 if `start_history_id` is too old to serve — the
        caller should fall back to a full sync in that case.
        """

        request = (
            self.service
            .users()
            .history()
            .list(
                userId="me",
                startHistoryId=start_history_id,
                pageToken=page_token,
            )
        )

        return self._execute_with_retry(request)

    def list_labels(self):
        """Return all labels on the account (system + user-created)."""

        request = (
            self.service
            .users()
            .labels()
            .list(userId="me")
        )

        response = self._execute_with_retry(request)

        return response.get("labels", [])

    def get_or_create_label(self, name):
        """
        Return the label id for `name`, creating the label if it does not
        exist yet. Idempotent: safe to call every run.
        """

        for label in self.list_labels():
            if label.get("name") == name:
                return label["id"]

        request = (
            self.service
            .users()
            .labels()
            .create(
                userId="me",
                body={
                    "name": name,
                    "labelListVisibility": "labelShow",
                    "messageListVisibility": "show",
                },
            )
        )

        created = self._execute_with_retry(request)

        return created["id"]

    def modify_message_labels(
        self,
        message_id,
        add_label_ids=None,
        remove_label_ids=None,
    ):
        """
        Add and/or remove labels on a single message. This is the only
        inbox-changing call in v1; it runs only after human approval.
        """

        request = (
            self.service
            .users()
            .messages()
            .modify(
                userId="me",
                id=message_id,
                body={
                    "addLabelIds": add_label_ids or [],
                    "removeLabelIds": remove_label_ids or [],
                },
            )
        )

        return self._execute_with_retry(request)

