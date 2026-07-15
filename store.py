"""SQLite state machine and transactional outbox."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .domain import compact_json, stable_hash


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


JSON_FIELDS = {"jira_payload", "slack_payload", "dedupe", "evidence", "analysis", "payload", "result"}


class StoreError(RuntimeError):
    pass


class IntakeStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.execute("PRAGMA busy_timeout = 30000")
        self._initialize()

    def close(self) -> None:
        self.conn.close()

    def _initialize(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS runs (
                id TEXT PRIMARY KEY,
                analyzer TEXT NOT NULL,
                prompt_version TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                status TEXT NOT NULL,
                requested_inputs INTEGER NOT NULL,
                processed INTEGER NOT NULL DEFAULT 0,
                skipped INTEGER NOT NULL DEFAULT 0,
                failed INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS run_transcripts (
                run_id TEXT NOT NULL REFERENCES runs(id),
                call_id TEXT NOT NULL,
                transcript_sha TEXT NOT NULL,
                status TEXT NOT NULL,
                duration_ms INTEGER,
                error TEXT,
                analysis TEXT,
                PRIMARY KEY (run_id, call_id, transcript_sha)
            );

            CREATE TABLE IF NOT EXISTS processed_inputs (
                transcript_sha TEXT NOT NULL,
                analyzer TEXT NOT NULL,
                prompt_version TEXT NOT NULL,
                call_id TEXT NOT NULL,
                first_run_id TEXT NOT NULL REFERENCES runs(id),
                processed_at TEXT NOT NULL,
                PRIMARY KEY (transcript_sha, analyzer, prompt_version)
            );

            CREATE TABLE IF NOT EXISTS review_items (
                id TEXT PRIMARY KEY,
                canonical_key TEXT NOT NULL UNIQUE,
                action TEXT NOT NULL CHECK (action IN ('file-new', 'corroborate')),
                target_issue TEXT,
                status TEXT NOT NULL CHECK (status IN ('pending', 'approved', 'rejected')),
                issue_type TEXT NOT NULL CHECK (issue_type IN ('Bug', 'Feature')),
                product_area TEXT NOT NULL,
                summary TEXT NOT NULL,
                description TEXT NOT NULL,
                severity TEXT NOT NULL,
                priority TEXT NOT NULL,
                priority_reason TEXT NOT NULL,
                confidence REAL NOT NULL,
                rationale TEXT NOT NULL,
                jira_payload TEXT,
                slack_payload TEXT NOT NULL,
                dedupe TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS sources (
                id TEXT PRIMARY KEY,
                review_id TEXT NOT NULL REFERENCES review_items(id),
                call_id TEXT NOT NULL,
                transcript_sha TEXT NOT NULL,
                transcript_path TEXT NOT NULL,
                account TEXT NOT NULL,
                owner TEXT NOT NULL,
                evidence TEXT NOT NULL,
                source_outcome TEXT NOT NULL,
                analysis TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE (review_id, transcript_sha, call_id)
            );

            CREATE TABLE IF NOT EXISTS decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                review_id TEXT NOT NULL REFERENCES review_items(id),
                decision TEXT NOT NULL,
                reason TEXT,
                decided_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS outbox (
                event_key TEXT PRIMARY KEY,
                review_id TEXT NOT NULL REFERENCES review_items(id),
                sink TEXT NOT NULL CHECK (sink IN ('jira', 'slack', 'corroboration')),
                payload TEXT NOT NULL,
                depends_on TEXT REFERENCES outbox(event_key),
                status TEXT NOT NULL CHECK (status IN ('pending', 'sending', 'delivered', 'error')),
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                result TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_review_status ON review_items(status);
            CREATE INDEX IF NOT EXISTS idx_sources_call ON sources(call_id);
            CREATE INDEX IF NOT EXISTS idx_outbox_status ON outbox(status);
            """
        )

    @contextmanager
    def transaction(self) -> Iterator[None]:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        else:
            self.conn.execute("COMMIT")

    def start_run(self, run_id: str, analyzer: str, prompt_version: str, requested_inputs: int) -> None:
        with self.transaction():
            self.conn.execute(
                "INSERT INTO runs(id, analyzer, prompt_version, started_at, status, requested_inputs) "
                "VALUES (?, ?, ?, ?, 'running', ?)",
                (run_id, analyzer, prompt_version, utc_now(), requested_inputs),
            )

    def finish_run(self, run_id: str, *, processed: int, skipped: int, failed: int) -> None:
        status = "completed" if failed == 0 else "completed_with_errors"
        with self.transaction():
            self.conn.execute(
                "UPDATE runs SET finished_at=?, status=?, processed=?, skipped=?, failed=? WHERE id=?",
                (utc_now(), status, processed, skipped, failed, run_id),
            )

    def is_processed(self, transcript_sha: str, analyzer: str, prompt_version: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM processed_inputs WHERE transcript_sha=? AND analyzer=? AND prompt_version=?",
            (transcript_sha, analyzer, prompt_version),
        ).fetchone()
        return row is not None

    def record_success(
        self,
        *,
        run_id: str,
        call_id: str,
        transcript_sha: str,
        analyzer: str,
        prompt_version: str,
        duration_ms: int,
        analysis: dict[str, Any],
    ) -> None:
        self.conn.execute(
            "INSERT INTO run_transcripts(run_id, call_id, transcript_sha, status, duration_ms, analysis) "
            "VALUES (?, ?, ?, 'processed', ?, ?)",
            (run_id, call_id, transcript_sha, duration_ms, compact_json(analysis)),
        )
        self.conn.execute(
            "INSERT OR IGNORE INTO processed_inputs "
            "(transcript_sha, analyzer, prompt_version, call_id, first_run_id, processed_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (transcript_sha, analyzer, prompt_version, call_id, run_id, utc_now()),
        )

    def record_skip(self, run_id: str, call_id: str, transcript_sha: str) -> None:
        with self.transaction():
            self.conn.execute(
                "INSERT INTO run_transcripts(run_id, call_id, transcript_sha, status) "
                "VALUES (?, ?, ?, 'skipped')",
                (run_id, call_id, transcript_sha),
            )

    def record_failure(
        self, run_id: str, call_id: str, transcript_sha: str, duration_ms: int, error: str
    ) -> None:
        with self.transaction():
            self.conn.execute(
                "INSERT INTO run_transcripts(run_id, call_id, transcript_sha, status, duration_ms, error) "
                "VALUES (?, ?, ?, 'error', ?, ?)",
                (run_id, call_id, transcript_sha, duration_ms, error[:4000]),
            )

    def insert_review_item(self, item: dict[str, Any]) -> bool:
        now = utc_now()
        cursor = self.conn.execute(
            """
            INSERT OR IGNORE INTO review_items(
                id, canonical_key, action, target_issue, status, issue_type, product_area,
                summary, description, severity, priority, priority_reason, confidence,
                rationale, jira_payload, slack_payload, dedupe, created_at, updated_at
            ) VALUES (?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                item["id"], item["canonical_key"], item["action"], item.get("target_issue"),
                item["issue_type"], item["product_area"], item["summary"], item["description"],
                item["severity"], item["priority"], item["priority_reason"], item["confidence"],
                item["rationale"],
                compact_json(item["jira_payload"]) if item.get("jira_payload") else None,
                compact_json(item["slack_payload"]), compact_json(item.get("dedupe", [])), now, now,
            ),
        )
        return cursor.rowcount == 1

    def add_source(self, review_id: str, source: dict[str, Any]) -> bool:
        source_id = stable_hash(
            review_id,
            source["transcript_sha"],
            source["call_id"],
            compact_json(source["evidence"]),
        )
        cursor = self.conn.execute(
            """
            INSERT OR IGNORE INTO sources(
                id, review_id, call_id, transcript_sha, transcript_path, account, owner,
                evidence, source_outcome, analysis, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                source_id, review_id, source["call_id"], source["transcript_sha"],
                source["transcript_path"], source["account"], source["owner"],
                compact_json(source["evidence"]), source["source_outcome"],
                compact_json(source["analysis"]), utc_now(),
            ),
        )
        return cursor.rowcount == 1

    def update_payloads(
        self, review_id: str, *, jira_payload: dict[str, Any] | None, slack_payload: dict[str, Any]
    ) -> None:
        self.conn.execute(
            "UPDATE review_items SET jira_payload=?, slack_payload=?, updated_at=? WHERE id=?",
            (
                compact_json(jira_payload) if jira_payload else None,
                compact_json(slack_payload),
                utc_now(),
                review_id,
            ),
        )

    def list_review_items(self, status: str | None = None) -> list[dict[str, Any]]:
        sql = (
            "SELECT r.*, COUNT(s.id) AS source_count FROM review_items r "
            "LEFT JOIN sources s ON s.review_id=r.id"
        )
        params: tuple[Any, ...] = ()
        if status:
            sql += " WHERE r.status=?"
            params = (status,)
        sql += " GROUP BY r.id ORDER BY r.created_at, r.id"
        return [self._decode(row) for row in self.conn.execute(sql, params).fetchall()]

    def get_review_item(self, review_id: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM review_items WHERE id=?", (review_id,)).fetchone()
        if not row:
            return None
        result = self._decode(row)
        source_rows = self.conn.execute(
            "SELECT * FROM sources WHERE review_id=? ORDER BY call_id, created_at", (review_id,)
        ).fetchall()
        result["sources"] = [self._decode(item) for item in source_rows]
        return result

    def source_outcomes(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT s.*, r.action, r.target_issue, r.issue_type, r.summary, r.priority,
                   r.id AS canonical_review_id
            FROM sources s JOIN review_items r ON r.id=s.review_id
            ORDER BY s.call_id, s.created_at
            """
        ).fetchall()
        return [self._decode(row) for row in rows]

    def approve(self, review_id: str) -> dict[str, Any]:
        with self.transaction():
            item = self.get_review_item(review_id)
            if not item:
                raise StoreError(f"unknown review item: {review_id}")
            if item["status"] == "rejected":
                raise StoreError("a rejected item cannot be approved")
            if item["status"] == "pending":
                self.conn.execute(
                    "UPDATE review_items SET status='approved', updated_at=? WHERE id=?",
                    (utc_now(), review_id),
                )
                self.conn.execute(
                    "INSERT INTO decisions(review_id, decision, decided_at) VALUES (?, 'approved', ?)",
                    (review_id, utc_now()),
                )

            first_sink = "jira" if item["action"] == "file-new" else "corroboration"
            first_payload = item["jira_payload"] if first_sink == "jira" else {
                "target_issue": item["target_issue"],
                "summary": item["summary"],
                "sources": [
                    {"call_id": source["call_id"], "account": source["account"], "evidence": source["evidence"]}
                    for source in item["sources"]
                ],
            }
            first_key = f"{review_id}:{first_sink}"
            self._enqueue(first_key, review_id, first_sink, first_payload, None)
            self._enqueue(
                f"{review_id}:slack",
                review_id,
                "slack",
                item["slack_payload"],
                first_key,
            )
        return self.get_review_item(review_id) or {}

    def reject(self, review_id: str, reason: str) -> None:
        if not reason.strip():
            raise StoreError("rejection reason is required")
        with self.transaction():
            row = self.conn.execute("SELECT status FROM review_items WHERE id=?", (review_id,)).fetchone()
            if not row:
                raise StoreError(f"unknown review item: {review_id}")
            if row["status"] == "approved":
                raise StoreError("an approved item cannot be rejected")
            self.conn.execute(
                "UPDATE review_items SET status='rejected', updated_at=? WHERE id=?",
                (utc_now(), review_id),
            )
            self.conn.execute(
                "INSERT INTO decisions(review_id, decision, reason, decided_at) "
                "VALUES (?, 'rejected', ?, ?)",
                (review_id, reason.strip(), utc_now()),
            )

    def _enqueue(
        self,
        event_key: str,
        review_id: str,
        sink: str,
        payload: dict[str, Any],
        depends_on: str | None,
    ) -> None:
        now = utc_now()
        self.conn.execute(
            """
            INSERT OR IGNORE INTO outbox(
                event_key, review_id, sink, payload, depends_on, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)
            """,
            (event_key, review_id, sink, compact_json(payload), depends_on, now, now),
        )

    def ready_outbox(self, max_attempts: int = 5) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT o.* FROM outbox o
            LEFT JOIN outbox d ON d.event_key=o.depends_on
            WHERE o.status IN ('pending', 'error') AND o.attempts < ?
              AND (o.depends_on IS NULL OR d.status='delivered')
            ORDER BY o.created_at, CASE o.sink WHEN 'jira' THEN 1 WHEN 'corroboration' THEN 1 ELSE 2 END
            """,
            (max_attempts,),
        ).fetchall()
        return [self._decode(row) for row in rows]

    def claim_outbox(self, event_key: str) -> bool:
        with self.transaction():
            cursor = self.conn.execute(
                "UPDATE outbox SET status='sending', attempts=attempts+1, updated_at=? "
                "WHERE event_key=? AND status IN ('pending', 'error')",
                (utc_now(), event_key),
            )
        return cursor.rowcount == 1

    def mark_delivered(self, event_key: str, result: dict[str, Any]) -> None:
        with self.transaction():
            self.conn.execute(
                "UPDATE outbox SET status='delivered', result=?, last_error=NULL, updated_at=? "
                "WHERE event_key=?",
                (compact_json(result), utc_now(), event_key),
            )

    def mark_outbox_error(self, event_key: str, error: str) -> None:
        with self.transaction():
            self.conn.execute(
                "UPDATE outbox SET status='error', last_error=?, updated_at=? WHERE event_key=?",
                (error[:4000], utc_now(), event_key),
            )

    def dependency_result(self, event: dict[str, Any]) -> dict[str, Any] | None:
        if not event.get("depends_on"):
            return None
        row = self.conn.execute(
            "SELECT result FROM outbox WHERE event_key=? AND status='delivered'",
            (event["depends_on"],),
        ).fetchone()
        return json.loads(row["result"]) if row and row["result"] else None

    def recover_interrupted_outbox(self) -> int:
        with self.transaction():
            cursor = self.conn.execute(
                "UPDATE outbox SET status='error', last_error='recovered interrupted delivery', updated_at=? "
                "WHERE status='sending'",
                (utc_now(),),
            )
        return cursor.rowcount

    def counts(self) -> dict[str, int]:
        queries = {
            "runs": "SELECT COUNT(*) FROM runs",
            "processed_inputs": "SELECT COUNT(*) FROM processed_inputs",
            "review_items": "SELECT COUNT(*) FROM review_items",
            "pending_review": "SELECT COUNT(*) FROM review_items WHERE status='pending'",
            "approved": "SELECT COUNT(*) FROM review_items WHERE status='approved'",
            "rejected": "SELECT COUNT(*) FROM review_items WHERE status='rejected'",
            "sources": "SELECT COUNT(*) FROM sources",
            "outbox_pending": "SELECT COUNT(*) FROM outbox WHERE status IN ('pending','sending','error')",
            "outbox_delivered": "SELECT COUNT(*) FROM outbox WHERE status='delivered'",
        }
        return {name: int(self.conn.execute(sql).fetchone()[0]) for name, sql in queries.items()}

    @staticmethod
    def _decode(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        for field in JSON_FIELDS & result.keys():
            if result[field] is not None and isinstance(result[field], str):
                try:
                    result[field] = json.loads(result[field])
                except json.JSONDecodeError:
                    pass
        return result
