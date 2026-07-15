"""Command-line review surface."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from .analyzers import AnalyzerError, analyzer_from_name
from .dedupe import load_existing_issues
from .evals import run_dev_eval
from .pipeline import EventLogger, IntakePipeline
from .sinks import StubDispatcher
from .store import IntakeStore, StoreError


EXERCISE_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_STATE = Path(os.getenv("BETTERBARK_STATE", EXERCISE_ROOT / "solution" / ".runtime" / "state.db"))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m solution",
        description="Human-gated customer-call product intake",
    )
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE, help="SQLite state path")
    parser.add_argument("--json", action="store_true", help="machine-readable command output")
    parser.add_argument("--quiet-logs", action="store_true", help="suppress structured event logs")
    subparsers = parser.add_subparsers(dest="command", required=True)

    scan = subparsers.add_parser("scan", help="analyze transcripts and stage review items; never writes sinks")
    scan.add_argument("paths", nargs="*", type=Path, help="transcript paths (default: all)")
    scan.add_argument("--provider", choices=("auto", "openai", "heuristic"), default="auto")
    scan.add_argument("--max-error-rate", type=float, default=0.05)

    queue = subparsers.add_parser("list", help="list human-review items")
    queue.add_argument("--status", choices=("pending", "approved", "rejected", "all"), default="pending")

    show = subparsers.add_parser("show", help="show one review item and its evidence")
    show.add_argument("review_id")

    approve = subparsers.add_parser("approve", help="approve one item, then dispatch its durable outbox")
    approve.add_argument("review_id")

    reject = subparsers.add_parser("reject", help="reject one review item")
    reject.add_argument("review_id")
    reject.add_argument("--reason", required=True)

    subparsers.add_parser("dispatch", help="retry approved, undelivered sink events")
    subparsers.add_parser("status", help="show run, queue, and outbox counts")

    evaluate = subparsers.add_parser("eval", help="run the labeled dev eval plus idempotency check")
    evaluate.add_argument("--provider", choices=("auto", "openai", "heuristic"), default="auto")
    evaluate.add_argument("--runs", type=int, default=1, help="independent runs for stability measurement")
    evaluate.add_argument("--details", action="store_true", help="include passing call details")
    return parser


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, default=str)


def _resolve_review_id(store: IntakeStore, value: str) -> str:
    if store.get_review_item(value):
        return value
    matches = [item["id"] for item in store.list_review_items() if item["id"].startswith(value)]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise StoreError(f"no review item matches {value!r}")
    raise StoreError(f"ambiguous review id prefix {value!r}: {matches}")


def _print_queue(items: list[dict[str, Any]]) -> None:
    if not items:
        print("No matching review items.")
        return
    print(f"{'ID':<28} {'STATUS':<9} {'ACTION':<13} {'PRI':<4} {'SRC':<3} SUMMARY")
    for item in items:
        print(
            f"{item['id']:<28} {item['status']:<9} {item['action']:<13} "
            f"{item['priority']:<4} {item['source_count']:<3} {item['summary']}"
        )


def _print_review(item: dict[str, Any]) -> None:
    print(f"{item['id']} [{item['status']}] {item['action']}")
    if item.get("target_issue"):
        print(f"Target: {item['target_issue']}")
    print(f"{item['issue_type']} | {item['product_area']} | {item['severity']}/{item['priority']}")
    print(f"Summary: {item['summary']}")
    print(f"Confidence: {item['confidence']:.2f}")
    print(f"Why: {item['rationale']}")
    print(f"Priority basis: {item['priority_reason']}")
    print("\nSources:")
    for source in item.get("sources", []):
        print(f"- {source['call_id']} | {source['account']} | owner={source['owner']}")
        for evidence in source["evidence"]:
            path = f"transcripts/{Path(source['transcript_path']).name}#L{evidence['line_start']}"
            print(f"  > [{evidence['speaker']}] {evidence['quote']}")
            print(f"    {path}")
    print("\nClosest duplicate candidates:")
    for candidate in item.get("dedupe", [])[:5]:
        print(
            f"- {candidate.get('source')}:{candidate.get('key')} "
            f"score={candidate.get('score')} status={candidate.get('status')} "
            f"{candidate.get('summary')}"
        )
    print("\nJira payload preview:")
    print(_json(item.get("jira_payload") or {"operation": "no new Jira; record corroboration"}))
    print("\nSlack payload preview:")
    print(_json(item["slack_payload"]))


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logger = EventLogger(enabled=not args.quiet_logs)
    try:
        if args.command == "eval":
            if args.runs < 1:
                raise ValueError("--runs must be at least 1")
            analyzer = analyzer_from_name(args.provider)
            result = run_dev_eval(
                EXERCISE_ROOT,
                analyzer,
                repeated_runs=args.runs,
                logs=not args.quiet_logs,
            )
            if args.json:
                print(_json(result))
            else:
                print(f"Analyzer: {result['analyzer']} ({result['prompt_version']})")
                print(f"Pass definition: {result['pass_definition']}")
                for report in result["runs"]:
                    print(
                        f"Run {report['repeat']}: {report['passed_calls']}/{report['total_calls']} calls, "
                        f"precision={report['precision']:.3f}, recall={report['recall']:.3f}, "
                        f"F1={report['f1']:.3f}, passed={report['passed']}"
                    )
                    for call_id, detail in report["details"].items():
                        if args.details or not detail["passed"]:
                            print(
                                f"  {call_id}: {'PASS' if detail['passed'] else 'FAIL'} "
                                f"expected={detail['expected_count']} predicted={detail['predicted_count']}"
                            )
                            if not detail["passed"]:
                                print("    " + _json(detail).replace("\n", "\n    "))
                print(
                    f"Stable case rate: {result['stable_case_rate']:.3f}; "
                    f"idempotency: {'PASS' if result['idempotency_passed'] else 'FAIL'}"
                )
            return 0 if result["all_runs_passed"] and result["idempotency_passed"] else 1

        store = IntakeStore(args.state)
        try:
            if args.command == "scan":
                analyzer = analyzer_from_name(args.provider)
                paths = args.paths or sorted((EXERCISE_ROOT / "transcripts").glob("call-*.md"))
                pipeline = IntakePipeline(
                    analyzer=analyzer,
                    store=store,
                    existing_issues=load_existing_issues(EXERCISE_ROOT / "data" / "existing_issues.json"),
                    logger=logger,
                )
                result = pipeline.scan(paths)
                print(_json(result.to_dict()) if args.json else (
                    f"Run {result.run_id}: processed={result.processed}, skipped={result.skipped}, "
                    f"failed={result.failed}, new_review_items={result.proposals_created}, "
                    f"sources_attached={result.sources_attached}, shipped_suppressed={result.suppressed_shipped}. "
                    "Nothing was sent; use `list` and `approve`."
                ))
                error_rate = result.failed / result.requested if result.requested else 0.0
                return 2 if error_rate > args.max_error_rate else 0

            if args.command == "list":
                items = store.list_review_items(None if args.status == "all" else args.status)
                if args.json:
                    print(_json(items))
                else:
                    _print_queue(items)
                return 0

            if args.command == "show":
                review_id = _resolve_review_id(store, args.review_id)
                item = store.get_review_item(review_id)
                assert item is not None
                if args.json:
                    print(_json(item))
                else:
                    _print_review(item)
                return 0

            if args.command == "approve":
                review_id = _resolve_review_id(store, args.review_id)
                item = store.approve(review_id)
                dispatch = StubDispatcher(store, logger).dispatch_all()
                result = {"review_id": review_id, "status": item["status"], "dispatch": dispatch}
                print(_json(result) if args.json else f"Approved {review_id}; dispatch={dispatch}")
                return 1 if dispatch["failed"] else 0

            if args.command == "reject":
                review_id = _resolve_review_id(store, args.review_id)
                store.reject(review_id, args.reason)
                result = {"review_id": review_id, "status": "rejected", "reason": args.reason}
                print(_json(result) if args.json else f"Rejected {review_id}: {args.reason}")
                return 0

            if args.command == "dispatch":
                result = StubDispatcher(store, logger).dispatch_all()
                print(_json(result) if args.json else f"Dispatch: {result}")
                return 1 if result["failed"] else 0

            if args.command == "status":
                result = store.counts()
                print(_json(result) if args.json else "\n".join(f"{key}: {value}" for key, value in result.items()))
                return 0
            raise AssertionError(f"unhandled command {args.command}")
        finally:
            store.close()
    except (AnalyzerError, StoreError, ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
