"""End-to-end intake pipeline. Scanning never writes to Jira or Slack."""

from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

from .analyzers import Analyzer
from .dedupe import (
    choose_existing_match,
    choose_review_cluster,
    confidence_band,
    priority_policy,
    tokenize,
)
from .domain import (
    ProposalValidationError,
    ProposedIssue,
    Transcript,
    parse_transcript,
    slugify,
    stable_hash,
    validate_and_ground_proposal,
)
from .observability import EventLogger, safe_error
from .store import IntakeStore


@dataclass
class RunSummary:
    run_id: str
    requested: int
    processed: int
    skipped: int
    failed: int
    proposals_created: int
    proposals_invalid: int
    model_proposals: int
    sources_attached: int
    suppressed_shipped: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class IntakePipeline:
    def __init__(
        self,
        *,
        analyzer: Analyzer,
        store: IntakeStore,
        existing_issues: list[dict[str, Any]],
        logger: EventLogger | None = None,
        max_transcript_bytes: int = 2_000_000,
    ):
        self.analyzer = analyzer
        self.store = store
        self.existing_issues = existing_issues
        self.logger = logger or EventLogger()
        self.max_transcript_bytes = max_transcript_bytes
        model = getattr(analyzer, "model", None)
        self.analyzer_id = f"{analyzer.name}:{model}" if model else analyzer.name

    def scan(self, paths: Iterable[str | Path]) -> RunSummary:
        ordered = sorted(
            {Path(path).resolve() for path in paths},
            key=lambda item: (item.name, str(item)),
        )
        run_id = f"run-{uuid.uuid4().hex[:12]}"
        self.store.start_run(run_id, self.analyzer_id, self.analyzer.prompt_version, len(ordered))
        self.logger.emit(
            "run_started",
            run_id=run_id,
            analyzer=self.analyzer_id,
            prompt_version=self.analyzer.prompt_version,
            requested=len(ordered),
        )

        processed = skipped = failed = proposals_created = proposals_invalid = 0
        model_proposals = sources_attached = suppressed_shipped = 0
        for path in ordered:
            started = time.monotonic()
            transcript: Transcript | None = None
            try:
                transcript = parse_transcript(path, max_bytes=self.max_transcript_bytes)
                if self.store.is_processed(
                    transcript.sha256, self.analyzer_id, self.analyzer.prompt_version
                ):
                    self.store.record_skip(run_id, transcript.call_id, transcript.sha256)
                    skipped += 1
                    self.logger.emit(
                        "transcript_skipped",
                        run_id=run_id,
                        call_id=transcript.call_id,
                        transcript_sha=transcript.sha256,
                        reason="already_processed",
                    )
                    continue

                analysis = self.analyzer.analyze(transcript, self.existing_issues)
                outcomes: list[dict[str, Any]] = []
                invalid: list[dict[str, str]] = []
                created_here = attached_here = shipped_here = 0
                with self.store.transaction():
                    for proposal in analysis.issues:
                        try:
                            proposal = validate_and_ground_proposal(transcript, proposal)
                        except ProposalValidationError as exc:
                            invalid.append({"summary": proposal.summary, "error": str(exc)})
                            self.logger.emit(
                                "proposal_rejected",
                                level="WARNING",
                                run_id=run_id,
                                call_id=transcript.call_id,
                                reason="provenance_or_schema_validation",
                                error_type=type(exc).__name__,
                                error=safe_error(exc),
                            )
                            continue
                        outcome = self._stage_proposal(transcript, proposal)
                        outcomes.append(outcome)
                        created_here += int(outcome["review_created"])
                        attached_here += int(outcome["source_attached"])
                        shipped_here += int(outcome["outcome"] == "suppressed-shipped")

                    duration_ms = int((time.monotonic() - started) * 1000)
                    result_record = analysis.to_dict()
                    result_record["pipeline_outcomes"] = outcomes
                    result_record["invalid_proposals"] = invalid
                    self.store.record_success(
                        run_id=run_id,
                        call_id=transcript.call_id,
                        transcript_sha=transcript.sha256,
                        analyzer=self.analyzer_id,
                        prompt_version=self.analyzer.prompt_version,
                        duration_ms=duration_ms,
                        analysis=result_record,
                    )
                processed += 1
                proposals_created += created_here
                proposals_invalid += len(invalid)
                model_proposals += len(analysis.issues)
                sources_attached += attached_here
                suppressed_shipped += shipped_here
                routing = (analysis.raw_response or {}).get("_routing", {})
                self.logger.emit(
                    "transcript_processed",
                    run_id=run_id,
                    call_id=transcript.call_id,
                    transcript_sha=transcript.sha256,
                    duration_ms=int((time.monotonic() - started) * 1000),
                    model_issue_count=len(analysis.issues),
                    ignored_count=len(analysis.ignored),
                    invalid_count=len(invalid),
                    selected_analyzer=routing.get("selected_analyzer", analysis.analyzer),
                    fallback_used=bool(routing.get("fallback_used", False)),
                    outcomes=outcomes,
                )
            except Exception as exc:
                failed += 1
                duration_ms = int((time.monotonic() - started) * 1000)
                call_id = transcript.call_id if transcript else path.stem
                transcript_sha = transcript.sha256 if transcript else stable_hash(str(path), length=64)
                error = f"{type(exc).__name__}: {safe_error(exc)}"
                self.store.record_failure(run_id, call_id, transcript_sha, duration_ms, error)
                self.logger.emit(
                    "transcript_failed",
                    level="ERROR",
                    run_id=run_id,
                    call_id=call_id,
                    source_file=path.name,
                    duration_ms=duration_ms,
                    error_type=type(exc).__name__,
                    error=safe_error(exc),
                )

        self.store.finish_run(
            run_id,
            processed=processed,
            skipped=skipped,
            failed=failed,
            model_proposals=model_proposals,
            invalid_proposals=proposals_invalid,
        )
        summary = RunSummary(
            run_id=run_id,
            requested=len(ordered),
            processed=processed,
            skipped=skipped,
            failed=failed,
            proposals_created=proposals_created,
            proposals_invalid=proposals_invalid,
            model_proposals=model_proposals,
            sources_attached=sources_attached,
            suppressed_shipped=suppressed_shipped,
        )
        self.logger.emit(
            "run_completed",
            level="WARNING" if failed or proposals_invalid else "INFO",
            **summary.to_dict(),
        )
        return summary

    def _stage_proposal(self, transcript: Transcript, proposal: ProposedIssue) -> dict[str, Any]:
        severity, priority, priority_reason = priority_policy(proposal)
        proposal.severity = severity
        existing_match, existing_ranked = choose_existing_match(proposal, self.existing_issues)
        dedupe_trace: list[dict[str, Any]] = [
            {"source": "existing", **item.to_dict()} for item in existing_ranked
        ]

        if existing_match and existing_match.status.lower() == "shipped":
            return {
                "outcome": "suppressed-shipped",
                "target": existing_match.key,
                "score": existing_match.score,
                "review_created": False,
                "source_attached": False,
            }

        if existing_match:
            action = "corroborate"
            target_issue = existing_match.key
            canonical_key = f"existing:{existing_match.key}"
            review_id = f"review-{stable_hash(canonical_key)}"
            source_outcome = "corroborate-existing"
            cluster_match = None
        else:
            cluster_match, cluster_ranked = choose_review_cluster(
                proposal, self.store.list_review_items()
            )
            dedupe_trace.extend(
                {"source": "review-queue", **item.to_dict()} for item in cluster_ranked
            )
            if cluster_match:
                clustered_item = self.store.get_review_item(cluster_match.key)
            else:
                clustered_item = None

            if clustered_item and clustered_item["status"] == "pending":
                review_id = clustered_item["id"]
                action = clustered_item["action"]
                target_issue = clustered_item.get("target_issue")
                canonical_key = clustered_item["canonical_key"]
                source_outcome = "corroborate-cluster"
            elif clustered_item:
                # A later report still needs its own human decision after the
                # original ticket was approved/rejected.
                action = "corroborate"
                target_issue = f"review:{clustered_item['id']}"
                canonical_key = f"late-corroboration:{clustered_item['id']}:{transcript.call_id}"
                review_id = f"review-{stable_hash(canonical_key)}"
                source_outcome = "corroborate-cluster"
            else:
                action = "file-new"
                target_issue = None
                signature = " ".join(sorted(tokenize(
                    f"{proposal.issue_type} {proposal.product_area} {proposal.summary}"
                )))
                canonical_key = f"new:{stable_hash(signature, transcript.call_id)}"
                review_id = f"review-{stable_hash(canonical_key)}"
                source_outcome = "file-new"

        item = {
            "id": review_id,
            "canonical_key": canonical_key,
            "action": action,
            "target_issue": target_issue,
            "issue_type": proposal.issue_type,
            "product_area": proposal.product_area,
            "summary": proposal.summary,
            "description": proposal.description,
            "severity": severity,
            "priority": priority,
            "priority_reason": priority_reason,
            "confidence": proposal.confidence,
            "rationale": proposal.rationale,
            "jira_payload": {},
            "slack_payload": {},
            "dedupe": dedupe_trace,
        }
        created = self.store.insert_review_item(item)
        source = {
            "call_id": transcript.call_id,
            "transcript_sha": transcript.sha256,
            "transcript_path": str(transcript.path),
            "account": transcript.account,
            "owner": transcript.owner,
            "evidence": [evidence.to_dict() for evidence in proposal.evidence],
            "source_outcome": source_outcome,
            "analysis": {
                "summary": proposal.summary,
                "description": proposal.description,
                "confidence": proposal.confidence,
                "confidence_band": confidence_band(proposal.confidence),
                "rationale": proposal.rationale,
                "duplicate_target": proposal.duplicate_target,
                "duplicate_rationale": proposal.duplicate_rationale,
                "dedupe_trace": dedupe_trace,
                "analyzer_metadata": proposal.analyzer_metadata,
            },
        }
        attached = self.store.add_source(review_id, source)
        self._refresh_payloads(review_id)
        return {
            "outcome": source_outcome,
            "review_id": review_id,
            "target": target_issue,
            "review_created": created,
            "source_attached": attached,
            "top_existing_score": existing_ranked[0].score if existing_ranked else None,
        }

    def _refresh_payloads(self, review_id: str) -> None:
        item = self.store.get_review_item(review_id)
        if not item:
            raise RuntimeError(f"review item disappeared during staging: {review_id}")
        source_sections: list[str] = []
        source_payloads: list[dict[str, Any]] = []
        owners: set[str] = set()
        for source in item["sources"]:
            owners.add(source["owner"])
            evidence_lines = []
            for evidence in source["evidence"]:
                relative = f"transcripts/{Path(source['transcript_path']).name}"
                link = f"{relative}#L{evidence['line_start']}-L{evidence['line_end']}"
                evidence_lines.append(
                    f"> [{evidence['speaker']}] {evidence['quote']}\n> Source: {link}"
                )
                source_payloads.append(
                    {
                        "call_id": source["call_id"],
                        "account": source["account"],
                        "owner": source["owner"],
                        "snippet": evidence["quote"],
                        "link": link,
                    }
                )
            source_sections.append(
                f"### {source['account']} ({source['call_id']})\n" + "\n".join(evidence_lines)
            )

        description = (
            f"## Customer problem\n{item['description']}\n\n"
            f"## Intake rationale\n{item['rationale']}\n\n"
            f"## First-pass impact\n{item['severity']} / {item['priority']}: "
            f"{item['priority_reason']}\n\n"
            f"## Verbatim external evidence\n" + "\n\n".join(source_sections)
        )
        jira_payload = None
        if item["action"] == "file-new":
            jira_payload = {
                "project": "PROJ",
                "type": item["issue_type"],
                "summary": item["summary"],
                "description": description,
                "priority": item["priority"],
                "severity": item["severity"],
                "source": {"reports": source_payloads},
                "idempotency_key": f"betterbark:{review_id}:jira",
            }

        owner_mentions = " ".join(f"@{slugify(owner)}" for owner in sorted(owners))
        channel = f"@{slugify(next(iter(owners)))}" if len(owners) == 1 else "#cs-product-intake"
        if item["action"] == "file-new":
            message = (
                f"{owner_mentions} Approved {item['issue_type'].lower()} from "
                f"{len(item['sources'])} customer call(s): {item['summary']} "
                f"[{item['priority']}]. Jira: {{JIRA_KEY}}."
            )
        else:
            message = (
                f"{owner_mentions} Approved corroboration for {item['target_issue']}: "
                f"{item['summary']} from {len(item['sources'])} call(s). No duplicate Jira created."
            )
        slack_payload = {
            "channel": channel,
            "text": message,
            "call_owners": sorted(owners),
            "source_call_ids": [source["call_id"] for source in item["sources"]],
            "idempotency_key": f"betterbark:{review_id}:slack",
        }
        self.store.update_payloads(review_id, jira_payload=jira_payload, slack_payload=slack_payload)
