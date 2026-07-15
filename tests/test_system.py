from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from solution.analyzers import HeuristicAnalyzer
from solution.dedupe import choose_existing_match, load_existing_issues, rank_existing
from solution.domain import (
    AnalysisResult,
    Evidence,
    IgnoredSignal,
    ProposalValidationError,
    ProposedIssue,
    ground_evidence,
    parse_transcript,
)
from solution.evals import run_dev_eval
from solution.pipeline import EventLogger, IntakePipeline
from solution.sinks import StubDispatcher
from solution.store import IntakeStore


ROOT = Path(__file__).resolve().parents[2]
TRANSCRIPTS = ROOT / "transcripts"
EXISTING = load_existing_issues(ROOT / "data" / "existing_issues.json")


def proposed(summary: str, description: str, quote: str = "evidence") -> ProposedIssue:
    return ProposedIssue(
        issue_type="Bug",
        summary=summary,
        product_area="Identity and access",
        description=description,
        severity="S3",
        confidence=0.9,
        evidence=[Evidence(quote, "Customer", 1, 1)],
        rationale="test",
    )


class ParserAndSafetyTests(unittest.TestCase):
    def test_parses_standard_and_internal_only_calls(self) -> None:
        normal = parse_transcript(TRANSCRIPTS / "call-001.md")
        internal = parse_transcript(TRANSCRIPTS / "call-007.md")
        self.assertEqual(normal.account, "Meridian Health")
        self.assertGreater(len(normal.external_turns), 0)
        self.assertEqual(internal.account, "Internal BetterBark")
        self.assertEqual(len(internal.external_turns), 0)

    def test_evidence_must_be_external_and_verbatim(self) -> None:
        transcript = parse_transcript(TRANSCRIPTS / "call-001.md")
        external = transcript.external_turns[0]
        grounded = ground_evidence(
            transcript,
            [Evidence(external.text, "wrong speaker", 999, 999)],
        )
        self.assertEqual(grounded[0].speaker, external.speaker)
        self.assertEqual(grounded[0].line_start, external.line_no)
        substring = external.text[: max(20, len(external.text) // 2)]
        grounded_substring = ground_evidence(
            transcript,
            [Evidence(substring, "wrong speaker", 999, 999)],
        )
        self.assertEqual(grounded_substring[0].quote, substring)
        with self.assertRaises(ProposalValidationError):
            ground_evidence(transcript, [Evidence("invented quote", "Dana", 1, 1)])

    def test_prompt_injection_is_ignored_but_real_issue_survives(self) -> None:
        transcript = parse_transcript(TRANSCRIPTS / "call-005.md")
        result = HeuristicAnalyzer().analyze(transcript, EXISTING)
        serialized = json.dumps(result.to_dict()).lower()
        self.assertEqual(len(result.issues), 1)
        self.assertIn("webhook", serialized)
        self.assertNotIn("wire transfer approval", serialized)


class DedupeTests(unittest.TestCase):
    def test_matches_android_launch_crash(self) -> None:
        item = proposed(
            "Android app crashes on launch after update",
            "After the 4.2 update the Android splash screen appears and the app closes; iOS is unaffected.",
        )
        match, _ = choose_existing_match(item, EXISTING)
        self.assertIsNotNone(match)
        self.assertEqual(match.key, "PROJ-110")

    def test_does_not_merge_azure_redirect_loop_into_okta_expiry(self) -> None:
        item = proposed(
            "Azure AD redirect loop after password change",
            "Azure AD users cannot enter the app after a password change until cookies are cleared. "
            "This is distinct from and not the Okta early-expiry behavior.",
        )
        match, _ = choose_existing_match(item, EXISTING)
        self.assertTrue(match is None or match.key != "PROJ-064")
        ranked = rank_existing(item, EXISTING, limit=len(EXISTING))
        okta = next(candidate for candidate in ranked if candidate.key == "PROJ-064")
        self.assertEqual(okta.score, 0.0)

    def test_dashboard_metric_bug_is_not_pdf_export_feature(self) -> None:
        item = proposed(
            "Usage dashboard active-member card is wrong",
            "The card says 280 while its per-team breakdown totals 412.",
        )
        match, _ = choose_existing_match(item, EXISTING)
        self.assertIsNone(match)


class PipelineReliabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="betterbark-test-")
        self.temp = Path(self.temporary.name)
        self.store = IntakeStore(self.temp / "state.db")
        self.logger = EventLogger(enabled=False)

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    def pipeline(self, analyzer: Any | None = None) -> IntakePipeline:
        return IntakePipeline(
            analyzer=analyzer or HeuristicAnalyzer(),
            store=self.store,
            existing_issues=EXISTING,
            logger=self.logger,
        )

    def test_second_scan_is_idempotent(self) -> None:
        paths = [TRANSCRIPTS / "call-001.md", TRANSCRIPTS / "call-006.md", TRANSCRIPTS / "call-012.md"]
        first = self.pipeline().scan(paths)
        before = self.store.counts()
        second = self.pipeline().scan(paths)
        after = self.store.counts()
        self.assertEqual(first.failed, 0)
        self.assertEqual(second.skipped, 3)
        self.assertEqual(before["review_items"], after["review_items"])
        self.assertEqual(before["sources"], after["sources"])

    def test_one_transcript_failure_does_not_rollback_another(self) -> None:
        class SelectiveFailure:
            name = "selective-failure"
            prompt_version = "test-v1"

            def analyze(self, transcript: Any, existing_issues: Any) -> AnalysisResult:
                if transcript.call_id == "call-002":
                    raise RuntimeError("injected analyzer failure")
                return AnalysisResult([], [IgnoredSignal("none", "test")], self.name, self.prompt_version)

        result = self.pipeline(SelectiveFailure()).scan(
            [TRANSCRIPTS / "call-001.md", TRANSCRIPTS / "call-002.md"]
        )
        self.assertEqual(result.processed, 1)
        self.assertEqual(result.failed, 1)
        self.assertEqual(self.store.counts()["processed_inputs"], 1)

    def test_scan_is_gated_and_approved_delivery_is_idempotent(self) -> None:
        self.pipeline().scan([TRANSCRIPTS / "call-001.md"])
        outbox = self.temp / "stub-outbox"
        self.assertFalse((outbox / "jira.jsonl").exists())
        item = self.store.list_review_items("pending")[0]
        self.store.approve(item["id"])

        def append_jira(payload: dict[str, Any]) -> dict[str, Any]:
            outbox.mkdir(parents=True, exist_ok=True)
            record = {"key": "PROJ-1001", **payload}
            with (outbox / "jira.jsonl").open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record) + "\n")
            return record

        def append_slack(payload: dict[str, Any]) -> dict[str, Any]:
            outbox.mkdir(parents=True, exist_ok=True)
            record = {"ts": "test-ts", **payload}
            with (outbox / "slack.jsonl").open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record) + "\n")
            return record

        dispatcher = StubDispatcher(
            self.store,
            self.logger,
            jira_create=append_jira,
            slack_post=append_slack,
            outbox_dir=outbox,
        )
        result = dispatcher.dispatch_all()
        self.assertEqual(result["delivered"], 2)
        jira_payload = json.loads((outbox / "jira.jsonl").read_text().strip())
        # A replay after an uncertain response finds the existing idempotency key.
        dispatcher._deliver_jira(dict(jira_payload))
        self.assertEqual(len((outbox / "jira.jsonl").read_text().splitlines()), 1)
        self.assertEqual(len((outbox / "slack.jsonl").read_text().splitlines()), 1)


class DevEvalTests(unittest.TestCase):
    def test_offline_baseline_passes_agreed_dev_contract(self) -> None:
        result = run_dev_eval(ROOT, HeuristicAnalyzer(), repeated_runs=2, logs=False)
        self.assertTrue(result["all_runs_passed"])
        self.assertTrue(result["idempotency_passed"])
        self.assertEqual(result["stable_case_rate"], 1.0)


if __name__ == "__main__":
    unittest.main()
