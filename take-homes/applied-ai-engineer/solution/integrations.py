from __future__ import annotations

import base64
import json
import os
import random
import time
import urllib.error
import urllib.request
import uuid
from email.message import Message
from typing import Any

from .config import jira_priority_names, slack_owner_ids
from .domain import slugify, stable_hash


RETRYABLE_HTTP_STATUS = {408, 409, 429, 500, 502, 503, 504}


class IntegrationError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        retryable: bool = False,
        retry_after: float | None = None,
    ):
        super().__init__(message)
        self.retryable = retryable
        self.retry_after = retry_after


def _attempts() -> int:
    try:
        value = int(os.getenv("SINK_HTTP_MAX_ATTEMPTS", "3"))
    except ValueError as exc:
        raise IntegrationError("SINK_HTTP_MAX_ATTEMPTS must be an integer") from exc
    if not 1 <= value <= 10:
        raise IntegrationError("SINK_HTTP_MAX_ATTEMPTS must be between 1 and 10")
    return value


def _timeout() -> float:
    try:
        value = float(os.getenv("SINK_HTTP_TIMEOUT_SECONDS", "20"))
    except ValueError as exc:
        raise IntegrationError("SINK_HTTP_TIMEOUT_SECONDS must be numeric") from exc
    if not 1 <= value <= 120:
        raise IntegrationError("SINK_HTTP_TIMEOUT_SECONDS must be between 1 and 120")
    return value


def _retry_delay(attempt: int, retry_after: float | None = None) -> float:
    if retry_after is not None:
        return min(60.0, max(0.0, retry_after))
    return min(30.0, 2 ** (attempt - 1) + random.random() * 0.25)


def _retry_after(headers: Message | None) -> float | None:
    if not headers:
        return None
    raw = headers.get("Retry-After")
    if raw is None:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _request_json(
    url: str,
    *,
    method: str,
    payload: dict[str, Any],
    headers: dict[str, str],
    timeout: float,
) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        method=method,
        headers={"Accept": "application/json", "Content-Type": "application/json", **headers},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        exc.read()
        request_id = exc.headers.get("x-request-id") if exc.headers else None
        suffix = f" request_id={request_id}" if request_id else ""
        raise IntegrationError(
            f"remote API returned HTTP {exc.code}{suffix}",
            retryable=exc.code in RETRYABLE_HTTP_STATUS,
            retry_after=_retry_after(exc.headers),
        ) from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise IntegrationError(
            f"remote API connection failed: {type(exc).__name__}", retryable=True
        ) from exc
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise IntegrationError("remote API returned invalid JSON", retryable=True) from exc
    if not isinstance(value, dict):
        raise IntegrationError("remote API returned a non-object JSON response")
    return value


def _adf_document(text: str) -> dict[str, Any]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    content: list[dict[str, Any]] = []
    for line in lines or ["No description supplied."]:
        if line.startswith("### "):
            content.append(
                {
                    "type": "heading",
                    "attrs": {"level": 3},
                    "content": [{"type": "text", "text": line[4:]}],
                }
            )
        elif line.startswith("## "):
            content.append(
                {
                    "type": "heading",
                    "attrs": {"level": 2},
                    "content": [{"type": "text", "text": line[3:]}],
                }
            )
        elif line.startswith("> "):
            content.append(
                {
                    "type": "blockquote",
                    "content": [
                        {
                            "type": "paragraph",
                            "content": [{"type": "text", "text": line[2:]}],
                        }
                    ],
                }
            )
        else:
            content.append(
                {"type": "paragraph", "content": [{"type": "text", "text": line}]}
            )
    return {
        "type": "doc",
        "version": 1,
        "content": content,
    }


class JiraCloudClient:
    def __init__(self) -> None:
        self.base_url = os.environ["JIRA_BASE_URL"].rstrip("/")
        self.project = os.environ["JIRA_PROJECT_KEY"]
        credential = f"{os.environ['JIRA_EMAIL']}:{os.environ['JIRA_API_TOKEN']}"
        encoded = base64.b64encode(credential.encode("utf-8")).decode("ascii")
        self.headers = {
            "Authorization": f"Basic {encoded}",
            "User-Agent": "betterbark-intake/1.0",
        }
        self.priority_names = jira_priority_names()
        self.max_attempts = _attempts()
        self.timeout = _timeout()

    def create_issue(self, payload: dict[str, Any]) -> dict[str, Any]:
        idempotency_key = str(payload.get("idempotency_key", ""))
        if not idempotency_key:
            raise IntegrationError("Jira payload is missing idempotency_key")
        label = "betterbark-" + stable_hash(idempotency_key, length=24)
        last_error: IntegrationError | None = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                existing = self._find_by_label(label)
                if existing:
                    existing_key = existing.get("key")
                    if not isinstance(existing_key, str) or not existing_key:
                        raise IntegrationError("Jira search result is missing the issue key")
                    return {
                        "key": existing_key,
                        "idempotency_key": idempotency_key,
                        "reconciled": True,
                    }
                response = _request_json(
                    f"{self.base_url}/rest/api/3/issue",
                    method="POST",
                    payload=self._create_payload(payload, label),
                    headers=self.headers,
                    timeout=self.timeout,
                )
                key = response.get("key")
                if not isinstance(key, str) or not key:
                    raise IntegrationError("Jira create response is missing the issue key")
                return {
                    "key": key,
                    "id": response.get("id"),
                    "self": response.get("self"),
                    "idempotency_key": idempotency_key,
                    "reconciled": False,
                }
            except IntegrationError as exc:
                last_error = exc
                if not exc.retryable or attempt == self.max_attempts:
                    raise
                time.sleep(_retry_delay(attempt, exc.retry_after))
        raise last_error or IntegrationError("Jira delivery failed")

    def _find_by_label(self, label: str) -> dict[str, Any] | None:
        response = _request_json(
            f"{self.base_url}/rest/api/3/search/jql",
            method="POST",
            payload={
                "jql": f'project = "{self.project}" AND labels = "{label}"',
                "fields": ["key", "summary"],
                "maxResults": 1,
            },
            headers=self.headers,
            timeout=self.timeout,
        )
        issues = response.get("issues", [])
        if not isinstance(issues, list):
            raise IntegrationError("Jira search response has an invalid issues field")
        return issues[0] if issues and isinstance(issues[0], dict) else None

    def _create_payload(self, payload: dict[str, Any], label: str) -> dict[str, Any]:
        fields: dict[str, Any] = {
            "project": {"key": self.project},
            "issuetype": {"name": str(payload["type"])},
            "summary": str(payload["summary"])[:255],
            "description": _adf_document(str(payload["description"])),
            "labels": [label, "betterbark-intake"],
        }
        priority = self.priority_names.get(str(payload.get("priority", "")))
        if priority:
            fields["priority"] = {"name": priority}
        return {
            "fields": fields,
            "properties": [
                {
                    "key": "betterbark.intake",
                    "value": {"idempotency_key": payload["idempotency_key"]},
                }
            ],
        }


class SlackClient:
    def __init__(self) -> None:
        self.token = os.environ["SLACK_BOT_TOKEN"]
        self.intake_channel = os.environ["SLACK_INTAKE_CHANNEL_ID"]
        self.owner_ids = slack_owner_ids()
        self.max_attempts = _attempts()
        self.timeout = _timeout()

    def post_message(self, payload: dict[str, Any]) -> dict[str, Any]:
        idempotency_key = str(payload.get("idempotency_key", ""))
        if not idempotency_key:
            raise IntegrationError("Slack payload is missing idempotency_key")
        owners = [str(owner) for owner in payload.get("call_owners", [])]
        channel = self.owner_ids.get(owners[0], self.intake_channel) if len(owners) == 1 else self.intake_channel
        text = str(payload.get("text", ""))
        for owner, slack_id in self.owner_ids.items():
            text = text.replace(f"@{slugify(owner)}", f"<@{slack_id}>")
        request_payload = {
            "channel": channel,
            "text": text,
            "client_msg_id": str(uuid.uuid5(uuid.NAMESPACE_URL, idempotency_key)),
            "unfurl_links": False,
            "unfurl_media": False,
            "metadata": {
                "event_type": "betterbark_intake",
                "event_payload": {"idempotency_hash": stable_hash(idempotency_key)},
            },
        }
        last_error: IntegrationError | None = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                response = _request_json(
                    "https://slack.com/api/chat.postMessage",
                    method="POST",
                    payload=request_payload,
                    headers={
                        "Authorization": f"Bearer {self.token}",
                        "User-Agent": "betterbark-intake/1.0",
                    },
                    timeout=self.timeout,
                )
                if not response.get("ok"):
                    code = str(response.get("error", "unknown_error"))
                    retryable = code in {
                        "ratelimited",
                        "internal_error",
                        "fatal_error",
                        "request_timeout",
                        "service_unavailable",
                    }
                    raise IntegrationError(
                        f"Slack rejected chat.postMessage: {code}", retryable=retryable
                    )
                return {
                    "ts": response.get("ts"),
                    "channel": response.get("channel", channel),
                    "idempotency_key": idempotency_key,
                }
            except IntegrationError as exc:
                last_error = exc
                if not exc.retryable or attempt == self.max_attempts:
                    raise
                time.sleep(_retry_delay(attempt, exc.retry_after))
        raise last_error or IntegrationError("Slack delivery failed")
