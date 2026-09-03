from __future__ import annotations

import io
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from solution.analyzers import (
    AnalyzerError,
    FallbackAnalyzer,
    HeuristicAnalyzer,
    OpenAICompatibleAnalyzer,
)
from solution.config import ConfigurationError, RuntimeConfig
from solution.dedupe import load_existing_issues
from solution.domain import (
    AnalysisResult,
    Evidence,
    IgnoredSignal,
    ProposedIssue,
    parse_transcript,
)
from solution.integrations import IntegrationError, JiraCloudClient, SlackClient
from solution.observability import EventLogger
from solution.pipeline import IntakePipeline
from solution.sinks import StubDispatcher
from solution.store import IntakeStore


ROOT = Path(__file__).resolve().parents[2]
TRANSCRIPTS = ROOT / "transcripts"
EXISTING = load_existing_issues(ROOT / "data" / "existing_issues.json")


class AnalyzerFallbackTests(unittest.TestCase):
    def test_model_failure_uses_visible_fallback(self) -> None:
        class FailingAnalyzer:
            name = "primary"
            prompt_version = "test-v1"

            def analyze(self, transcript: object, existing: object) -> AnalysisResult:
                raise AnalyzerError("temporary model failure")

        stream = io.StringIO()
        logger = EventLogger(stream=stream)
        analyzer = FallbackAnalyzer(FailingAnalyzer(), [HeuristicAnalyzer()], logger)
        result = analyzer.analyze(parse_transcript(TRANSCRIPTS / "call-001.md"), EXISTING)
        routing = (result.raw_response or {})["_routing"]
        self.assertTrue(routing["fallback_used"])
        self.assertEqual(routing["selected_analyzer"], "heuristic-baseline")
        self.assertIn("analyzer_fallback_selected", stream.getvalue())

    def test_programming_error_is_not_hidden_by_fallback(self) -> None:
        class BrokenAnalyzer:
            name = "broken"
            prompt_version = "test-v1"

            def analyze(self, transcript: object, existing: object) -> AnalysisResult:
                raise RuntimeError("programming defect")

        analyzer = FallbackAnalyzer(
            BrokenAnalyzer(), [HeuristicAnalyzer()], EventLogger(enabled=False)
        )
        with self.assertRaisesRegex(RuntimeError, "programming defect"):
            analyzer.analyze(parse_transcript(TRANSCRIPTS / "call-001.md"), EXISTING)

    def test_circuit_breaker_stops_hammering_failed_primary(self) -> None:
        class Primary:
            name = "primary"
            prompt_version = "test-v1"
            calls = 0

            def analyze(self, transcript: object, existing: object) -> AnalysisResult:
                self.calls += 1
                raise AnalyzerError("provider unavailable")

        class Secondary:
            name = "secondary"
            prompt_version = "test-v1"

            def analyze(self, transcript: object, existing: object) -> AnalysisResult:
                return AnalysisResult(
                    [], [IgnoredSignal("none", "test")], self.name, self.prompt_version
                )

        primary = Primary()
        analyzer = FallbackAnalyzer(
            primary,
            [Secondary()],
            EventLogger(enabled=False),
            failure_threshold=1,
            cooldown_seconds=60,
        )
        transcript = parse_transcript(TRANSCRIPTS / "call-001.md")
        analyzer.analyze(transcript, EXISTING)
        second = analyzer.analyze(transcript, EXISTING)
        self.assertEqual(primary.calls, 1)
        self.assertEqual(
            (second.raw_response or {})["_routing"]["failed_attempts"][0]["error_type"],
            "CircuitOpen",
        )

    def test_fallback_route_is_attached_to_review_source(self) -> None:
        class Primary:
            name = "primary"
            prompt_version = "test-v1"

            def analyze(self, transcript: object, existing: object) -> AnalysisResult:
                raise AnalyzerError("provider unavailable")

        with tempfile.TemporaryDirectory(prefix="betterbark-fallback-review-") as temporary:
            store = IntakeStore(Path(temporary) / "state.db")
            try:
                analyzer = FallbackAnalyzer(
                    Primary(), [HeuristicAnalyzer()], EventLogger(enabled=False)
                )
                IntakePipeline(
                    analyzer=analyzer,
                    store=store,
                    existing_issues=EXISTING,
                    logger=EventLogger(enabled=False),
                ).scan([TRANSCRIPTS / "call-001.md"])
                item = store.get_review_item(store.list_review_items()[0]["id"])
                routing = item["sources"][0]["analysis"]["analyzer_metadata"]["routing"]
                self.assertTrue(routing["fallback_used"])
                self.assertEqual(routing["selected_analyzer"], "heuristic-baseline")
            finally:
                store.close()

    def test_model_schema_failure_is_retryable_by_fallback_layer(self) -> None:
        transcript = parse_transcript(TRANSCRIPTS / "call-001.md")
        turn = transcript.external_turns[0]
        response = {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "issues": [
                                    {
                                        "issue_type": "Bug",
                                        "summary": "Looks valid",
                                        "product_area": "Reporting",
                                        "description": "",
                                        "severity": "S3",
                                        "confidence": 0.9,
                                        "evidence": [
                                            {
                                                "quote": turn.text,
                                                "speaker": turn.speaker,
                                                "line_start": turn.line_no,
                                                "line_end": turn.line_no,
                                            }
                                        ],
                                        "rationale": "test",
                                    }
                                ],
                                "ignored": [],
                            }
                        )
                    }
                }
            ]
        }
        analyzer = OpenAICompatibleAnalyzer(api_key="test", max_attempts=1)
        with patch.object(analyzer, "_post_json", return_value=response):
            with self.assertRaisesRegex(AnalyzerError, "deterministic validation"):
                analyzer.analyze(transcript, EXISTING)


class ConfigurationTests(unittest.TestCase):
    def test_production_fails_closed_without_model_and_live_sinks(self) -> None:
        with patch.dict(os.environ, {"BETTERBARK_ENV": "production"}, clear=True):
            config = RuntimeConfig.from_env(ROOT)
            with self.assertRaisesRegex(ConfigurationError, "heuristic"):
                config.validate_provider("heuristic")
            with self.assertRaisesRegex(ConfigurationError, "OPENAI_API_KEY"):
                config.validate_provider("openai")

        production = {
            "BETTERBARK_ENV": "production",
            "OPENAI_API_KEY": "test-key",
            "OPENAI_MODEL": "primary-model",
            "OPENAI_FALLBACK_MODEL": "fallback-model",
        }
        with patch.dict(os.environ, production, clear=True):
            config = RuntimeConfig.from_env(ROOT)
            config.validate_provider("openai")
            with self.assertRaisesRegex(ConfigurationError, "SINK_MODE=live"):
                config.validate_live_sinks()

    def test_invalid_boolean_and_retry_budget_are_rejected(self) -> None:
        with patch.dict(
            os.environ,
            {"ALLOW_HEURISTIC_FALLBACK": "sometimes"},
            clear=True,
        ):
            with self.assertRaises(ConfigurationError):
                RuntimeConfig.from_env(ROOT)
        with patch.dict(os.environ, {"OUTBOX_MAX_ATTEMPTS": "0"}, clear=True):
            with self.assertRaises(ConfigurationError):
                RuntimeConfig.from_env(ROOT)

    def test_existing_issue_snapshot_rejects_duplicate_keys(self) -> None:
        with tempfile.TemporaryDirectory(prefix="betterbark-existing-") as temporary:
            path = Path(temporary) / "existing.json"
            path.write_text(
                json.dumps(
                    [
                        {"key": "PROJ-1", "summary": "One", "status": "Open"},
                        {"key": "PROJ-1", "summary": "Two", "status": "Open"},
                    ]
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "duplicated"):
                load_existing_issues(path)


class PipelineBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="betterbark-boundary-")
        self.temp = Path(self.temporary.name)
        self.store = IntakeStore(self.temp / "state.db")

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    def pipeline(self, max_bytes: int = 2_000_000) -> IntakePipeline:
        return IntakePipeline(
            analyzer=HeuristicAnalyzer(),
            store=self.store,
            existing_issues=EXISTING,
            logger=EventLogger(enabled=False),
            max_transcript_bytes=max_bytes,
        )

    def test_malformed_and_oversized_inputs_are_isolated(self) -> None:
        malformed = self.temp / "malformed.md"
        malformed.write_text("not a transcript\n", encoding="utf-8")
        oversized = self.temp / "oversized.md"
        oversized.write_text("x" * 20_000, encoding="utf-8")
        invalid_utf8 = self.temp / "invalid-utf8.md"
        invalid_utf8.write_bytes(b"\xff\xfe\x00")
        result = self.pipeline(max_bytes=15_000).scan(
            [malformed, oversized, invalid_utf8, TRANSCRIPTS / "call-001.md"]
        )
        self.assertEqual(result.processed, 1)
        self.assertEqual(result.failed, 3)
        self.assertEqual(self.store.counts()["processed_inputs"], 1)

    def test_duplicate_content_in_two_paths_does_not_crash_run(self) -> None:
        first = self.temp / "copy-a.md"
        second = self.temp / "copy-b.md"
        content = (TRANSCRIPTS / "call-001.md").read_text(encoding="utf-8")
        first.write_text(content, encoding="utf-8")
        second.write_text(content, encoding="utf-8")
        result = self.pipeline().scan([first, second])
        self.assertEqual(result.processed, 1)
        self.assertEqual(result.skipped, 1)
        self.assertEqual(result.failed, 0)

    def test_repeated_rejection_is_idempotent(self) -> None:
        self.pipeline().scan([TRANSCRIPTS / "call-001.md"])
        review_id = self.store.list_review_items("pending")[0]["id"]
        self.store.reject(review_id, "not actionable")
        self.store.reject(review_id, "not actionable")
        count = self.store.conn.execute(
            "SELECT COUNT(*) FROM decisions WHERE review_id=?", (review_id,)
        ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_invalid_model_proposal_is_quarantined_and_counted(self) -> None:
        transcript = parse_transcript(TRANSCRIPTS / "call-001.md")
        turn = transcript.external_turns[0]

        class InvalidProposalAnalyzer:
            name = "invalid-proposal"
            prompt_version = "test-v1"

            def analyze(self, transcript: object, existing: object) -> AnalysisResult:
                issue = ProposedIssue(
                    issue_type="Bug",
                    summary="A valid-looking title",
                    product_area="Reporting",
                    description="",
                    severity="S3",
                    confidence=0.9,
                    evidence=[Evidence(turn.text, turn.speaker, turn.line_no, turn.line_no)],
                    rationale="test",
                )
                return AnalysisResult([issue], [], self.name, self.prompt_version)

        pipeline = IntakePipeline(
            analyzer=InvalidProposalAnalyzer(),
            store=self.store,
            existing_issues=EXISTING,
            logger=EventLogger(enabled=False),
        )
        result = pipeline.scan([TRANSCRIPTS / "call-001.md"])
        self.assertEqual(result.processed, 1)
        self.assertEqual(result.proposals_invalid, 1)
        self.assertEqual(self.store.counts()["invalid_proposals"], 1)
        self.assertEqual(self.store.counts()["review_items"], 0)


class DeadLetterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="betterbark-dead-letter-")
        self.temp = Path(self.temporary.name)
        self.store = IntakeStore(self.temp / "state.db")
        pipeline = IntakePipeline(
            analyzer=HeuristicAnalyzer(),
            store=self.store,
            existing_issues=EXISTING,
            logger=EventLogger(enabled=False),
        )
        pipeline.scan([TRANSCRIPTS / "call-001.md"])
        self.review_id = self.store.list_review_items("pending")[0]["id"]
        self.store.approve(self.review_id)

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    def test_failure_gets_one_attempt_per_dispatch_then_dead_letters(self) -> None:
        calls = 0

        def fail_jira(payload: dict[str, object]) -> dict[str, object]:
            nonlocal calls
            calls += 1
            raise RuntimeError("Jira unavailable")

        dispatcher = StubDispatcher(
            self.store,
            EventLogger(enabled=False),
            jira_create=fail_jira,
            slack_post=lambda payload: {"ts": "unused"},
            local_reconciliation=False,
            max_attempts=2,
        )
        first = dispatcher.dispatch_all()
        self.assertEqual(first["failed"], 1)
        self.assertEqual(calls, 1)
        event = self.store.get_outbox(f"{self.review_id}:jira")
        self.assertEqual(event["status"], "error")

        second = dispatcher.dispatch_all()
        self.assertEqual(second["dead_lettered"], 1)
        self.assertEqual(calls, 2)
        self.assertEqual(self.store.counts()["outbox_dead"], 1)
        self.assertEqual(self.store.counts()["outbox_blocked"], 1)
        dead_letters = self.store.list_dead_letters()
        self.assertEqual(dead_letters[0]["event_key"], f"{self.review_id}:jira")
        self.assertNotIn("payload", dead_letters[0])

        third = dispatcher.dispatch_all()
        self.assertEqual(third["failed"], 0)
        self.assertEqual(calls, 2)

        self.store.retry_dead(f"{self.review_id}:jira")
        recovery = StubDispatcher(
            self.store,
            EventLogger(enabled=False),
            jira_create=lambda payload: {"key": "PROJ-2001"},
            slack_post=lambda payload: {"ts": "123.456"},
            local_reconciliation=False,
            max_attempts=2,
        ).dispatch_all()
        self.assertEqual(recovery["delivered"], 2)
        self.assertEqual(self.store.counts()["outbox_dead"], 0)
        self.assertEqual(self.store.counts()["outbox_pending"], 0)

    def test_old_outbox_schema_is_migrated(self) -> None:
        legacy = self.temp / "legacy.db"
        connection = sqlite3.connect(legacy)
        connection.execute(
            "CREATE TABLE outbox (event_key TEXT PRIMARY KEY, review_id TEXT NOT NULL, "
            "sink TEXT NOT NULL, payload TEXT NOT NULL, depends_on TEXT, "
            "status TEXT NOT NULL CHECK(status IN ('pending','sending','delivered','error')), "
            "attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT, result TEXT, "
            "created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
        )
        connection.close()
        migrated = IntakeStore(legacy)
        try:
            schema = migrated.conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='outbox'"
            ).fetchone()[0]
            self.assertIn("'dead'", schema)
        finally:
            migrated.close()

    def test_slack_retry_does_not_repeat_delivered_jira(self) -> None:
        jira_calls = 0
        slack_calls = 0

        def jira(payload: dict[str, object]) -> dict[str, object]:
            nonlocal jira_calls
            jira_calls += 1
            return {"key": "PROJ-3001"}

        def slack(payload: dict[str, object]) -> dict[str, object]:
            nonlocal slack_calls
            slack_calls += 1
            if slack_calls == 1:
                raise RuntimeError("Slack unavailable")
            return {"ts": "456.789"}

        dispatcher = StubDispatcher(
            self.store,
            EventLogger(enabled=False),
            jira_create=jira,
            slack_post=slack,
            local_reconciliation=False,
            max_attempts=3,
        )
        first = dispatcher.dispatch_all()
        second = dispatcher.dispatch_all()
        self.assertEqual(first["delivered"], 1)
        self.assertEqual(first["failed"], 1)
        self.assertEqual(second["delivered"], 1)
        self.assertEqual(jira_calls, 1)
        self.assertEqual(slack_calls, 2)


class ObservabilityTests(unittest.TestCase):
    def test_secrets_are_redacted_from_keys_and_free_text(self) -> None:
        stream = io.StringIO()
        logger = EventLogger(stream=stream)
        logger.emit(
            "failure",
            api_key="sk-super-secret-value",
            error="Authorization: Bearer sk-another-secret-value",
        )
        record = json.loads(stream.getvalue())
        self.assertEqual(record["api_key"], "[REDACTED]")
        self.assertNotIn("super-secret", stream.getvalue())
        self.assertNotIn("another-secret", stream.getvalue())


class LiveIntegrationContractTests(unittest.TestCase):
    def test_jira_uses_adf_property_and_reconciliation_label(self) -> None:
        environment = {
            "JIRA_BASE_URL": "https://example.atlassian.net",
            "JIRA_EMAIL": "operator@example.com",
            "JIRA_API_TOKEN": "secret-token",
            "JIRA_PROJECT_KEY": "PROJ",
            "SINK_HTTP_MAX_ATTEMPTS": "1",
        }
        payload = {
            "type": "Bug",
            "summary": "Broken dashboard",
            "description": "First line\nSecond line",
            "priority": "P2",
            "idempotency_key": "betterbark:review-1:jira",
        }
        with patch.dict(os.environ, environment, clear=True), patch(
            "solution.integrations._request_json",
            side_effect=[{"issues": []}, {"key": "PROJ-101", "id": "101"}],
        ) as request:
            result = JiraCloudClient().create_issue(payload)
        self.assertEqual(result["key"], "PROJ-101")
        search_payload = request.call_args_list[0].kwargs["payload"]
        create_payload = request.call_args_list[1].kwargs["payload"]
        label = create_payload["fields"]["labels"][0]
        self.assertIn(label, search_payload["jql"])
        self.assertEqual(create_payload["fields"]["description"]["type"], "doc")
        self.assertEqual(
            create_payload["properties"][0]["value"]["idempotency_key"],
            payload["idempotency_key"],
        )

    def test_jira_reconciles_existing_issue_without_create(self) -> None:
        environment = {
            "JIRA_BASE_URL": "https://example.atlassian.net",
            "JIRA_EMAIL": "operator@example.com",
            "JIRA_API_TOKEN": "secret-token",
            "JIRA_PROJECT_KEY": "PROJ",
            "SINK_HTTP_MAX_ATTEMPTS": "1",
        }
        with patch.dict(os.environ, environment, clear=True), patch(
            "solution.integrations._request_json",
            return_value={"issues": [{"key": "PROJ-99"}]},
        ) as request:
            result = JiraCloudClient().create_issue(
                {
                    "type": "Feature",
                    "summary": "Request",
                    "description": "Description",
                    "idempotency_key": "key-1",
                }
            )
        self.assertTrue(result["reconciled"])
        self.assertEqual(result["key"], "PROJ-99")
        self.assertEqual(request.call_count, 1)

    def test_jira_reconciles_after_ambiguous_create_timeout(self) -> None:
        environment = {
            "JIRA_BASE_URL": "https://example.atlassian.net",
            "JIRA_EMAIL": "operator@example.com",
            "JIRA_API_TOKEN": "secret-token",
            "JIRA_PROJECT_KEY": "PROJ",
            "SINK_HTTP_MAX_ATTEMPTS": "2",
        }
        responses = [
            {"issues": []},
            IntegrationError("connection lost", retryable=True),
            {"issues": [{"key": "PROJ-102"}]},
        ]
        with patch.dict(os.environ, environment, clear=True), patch(
            "solution.integrations._request_json", side_effect=responses
        ) as request, patch("solution.integrations.time.sleep"):
            result = JiraCloudClient().create_issue(
                {
                    "type": "Bug",
                    "summary": "Request",
                    "description": "Description",
                    "idempotency_key": "key-timeout",
                }
            )
        self.assertTrue(result["reconciled"])
        self.assertEqual(result["key"], "PROJ-102")
        self.assertEqual(request.call_count, 3)

    def test_slack_resolves_owner_id_and_sends_stable_message_id(self) -> None:
        environment = {
            "SLACK_BOT_TOKEN": "xoxb-test-token",
            "SLACK_INTAKE_CHANNEL_ID": "CINTAKE",
            "SLACK_OWNER_IDS_JSON": json.dumps({"Alex Owner": "UOWNER"}),
            "SINK_HTTP_MAX_ATTEMPTS": "1",
        }
        payload = {
            "text": "@alex-owner Approved issue",
            "call_owners": ["Alex Owner"],
            "idempotency_key": "betterbark:review-1:slack",
        }
        with patch.dict(os.environ, environment, clear=True), patch(
            "solution.integrations._request_json",
            return_value={"ok": True, "ts": "123.456", "channel": "UOWNER"},
        ) as request:
            result = SlackClient().post_message(payload)
        sent = request.call_args.kwargs["payload"]
        self.assertEqual(result["channel"], "UOWNER")
        self.assertEqual(sent["channel"], "UOWNER")
        self.assertIn("<@UOWNER>", sent["text"])
        self.assertEqual(len(sent["client_msg_id"]), 36)


if __name__ == "__main__":
    unittest.main()
