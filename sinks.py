"""Approved-event dispatcher with crash-safe idempotency for the naive stubs."""

from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

from stubs import jira_stub, slack_stub

from .domain import stable_hash
from .pipeline import EventLogger
from .store import IntakeStore


class SinkError(RuntimeError):
    pass


@contextmanager
def _file_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        try:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        except ImportError:  # pragma: no cover - Linux is used by the exercise
            pass
        try:
            yield
        finally:
            try:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except ImportError:  # pragma: no cover
                pass


def _find_existing(path: Path, idempotency_key: str) -> dict[str, Any] | None:
    if not path.exists():
        return None
    with path.open(encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise SinkError(f"cannot safely reconcile corrupt {path} line {line_no}") from exc
            if record.get("idempotency_key") == idempotency_key:
                return record
    return None


class StubDispatcher:
    def __init__(
        self,
        store: IntakeStore,
        logger: EventLogger | None = None,
        *,
        jira_create: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
        slack_post: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
        outbox_dir: Path | None = None,
    ):
        self.store = store
        self.logger = logger or EventLogger()
        stub_dir = Path(jira_stub.__file__).resolve().parent
        target_outbox = outbox_dir or (stub_dir / "outbox")
        self.jira_log = target_outbox / "jira.jsonl"
        self.slack_log = target_outbox / "slack.jsonl"
        self.lock_path = target_outbox / ".dispatch.lock"
        self.jira_create = jira_create or jira_stub.create_issue
        self.slack_post = slack_post or slack_stub.post_message

    def dispatch_all(self) -> dict[str, int]:
        recovered = self.store.recover_interrupted_outbox()
        delivered = failed = 0
        while True:
            ready = self.store.ready_outbox()
            if not ready:
                break
            progressed = False
            for event in ready:
                if not self.store.claim_outbox(event["event_key"]):
                    continue
                progressed = True
                try:
                    result = self._deliver(event)
                except Exception as exc:
                    failed += 1
                    self.store.mark_outbox_error(event["event_key"], repr(exc))
                    self.logger.emit(
                        "sink_delivery_failed",
                        event_key=event["event_key"],
                        review_id=event["review_id"],
                        sink=event["sink"],
                        error=repr(exc),
                    )
                else:
                    delivered += 1
                    self.store.mark_delivered(event["event_key"], result)
                    self.logger.emit(
                        "sink_delivered",
                        event_key=event["event_key"],
                        review_id=event["review_id"],
                        sink=event["sink"],
                        result_reference=result.get("key") or result.get("ts") or result.get("target_issue"),
                    )
            if not progressed:
                break
        return {"recovered": recovered, "delivered": delivered, "failed": failed}

    def _deliver(self, event: dict[str, Any]) -> dict[str, Any]:
        payload = dict(event["payload"])
        if event["sink"] == "jira":
            return self._deliver_jira(payload)
        if event["sink"] == "slack":
            dependency = self.store.dependency_result(event) or {}
            reference = dependency.get("key") or dependency.get("target_issue") or "recorded"
            payload["text"] = str(payload["text"]).replace("{JIRA_KEY}", str(reference))
            return self._deliver_slack(payload)
        if event["sink"] == "corroboration":
            return {
                "target_issue": payload.get("target_issue"),
                "corroboration_id": "corr-" + stable_hash(event["event_key"]),
                "source_count": len(payload.get("sources", [])),
                "recorded": True,
            }
        raise SinkError(f"unsupported sink: {event['sink']}")

    def _deliver_jira(self, payload: dict[str, Any]) -> dict[str, Any]:
        key = str(payload.get("idempotency_key", ""))
        if not key:
            raise SinkError("Jira payload missing idempotency_key")
        with _file_lock(self.lock_path):
            existing = _find_existing(self.jira_log, key)
            if existing:
                return existing
            return self.jira_create(payload)

    def _deliver_slack(self, payload: dict[str, Any]) -> dict[str, Any]:
        key = str(payload.get("idempotency_key", ""))
        if not key:
            raise SinkError("Slack payload missing idempotency_key")
        with _file_lock(self.lock_path):
            existing = _find_existing(self.slack_log, key)
            if existing:
                return existing
            return self.slack_post(payload)
