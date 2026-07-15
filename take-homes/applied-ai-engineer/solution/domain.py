"""Domain objects and the deterministic transcript parser.

The parser is intentionally not delegated to a model. Speaker role, source line,
account, owner, and transcript hash are control-plane data and must be exact.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable


HEADER_RE = re.compile(
    r"^#\s+Call\s+[—-]\s+(?P<account>.+?)\s+[×x]\s+BetterBark\s+[·-]\s+(?P<kind>.+?)\s*$"
)
GENERIC_HEADER_RE = re.compile(r"^#\s+Call\s+[—-]\s+(?P<title>.+?)\s*$")
META_RE = re.compile(r"^Date:\s*(?P<date>.+?)\s+[·-]\s+Call ID:\s*(?P<call_id>call-\d+)\s*$")
TURN_RE = re.compile(r"^\[(?P<role>EXTERNAL|INTERNAL)]\s+(?P<speaker>[^:]+):\s*(?P<text>.*)$")


class TranscriptParseError(ValueError):
    """Raised when source metadata cannot be parsed safely."""


class ProposalValidationError(ValueError):
    """Raised when model output cannot be grounded in the source transcript."""


@dataclass(frozen=True)
class Turn:
    role: str
    speaker: str
    text: str
    line_no: int


@dataclass(frozen=True)
class Transcript:
    call_id: str
    account: str
    call_kind: str
    date: str
    owner: str
    path: Path
    raw: str
    turns: tuple[Turn, ...]
    sha256: str

    @property
    def external_turns(self) -> tuple[Turn, ...]:
        return tuple(turn for turn in self.turns if turn.role == "EXTERNAL")

    @property
    def internal_turns(self) -> tuple[Turn, ...]:
        return tuple(turn for turn in self.turns if turn.role == "INTERNAL")

    def numbered_source(self) -> str:
        return "\n".join(f"L{number}: {line}" for number, line in enumerate(self.raw.splitlines(), 1))


@dataclass(frozen=True)
class Evidence:
    quote: str
    speaker: str
    line_start: int
    line_end: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ProposedIssue:
    issue_type: str
    summary: str
    product_area: str
    description: str
    severity: str
    confidence: float
    evidence: list[Evidence]
    rationale: str
    duplicate_target: str | None = None
    duplicate_rationale: str | None = None
    analyzer_metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["evidence"] = [item.to_dict() for item in self.evidence]
        return value


@dataclass
class IgnoredSignal:
    signal: str
    reason: str


@dataclass
class AnalysisResult:
    issues: list[ProposedIssue]
    ignored: list[IgnoredSignal]
    analyzer: str
    prompt_version: str
    raw_response: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "issues": [issue.to_dict() for issue in self.issues],
            "ignored": [asdict(item) for item in self.ignored],
            "analyzer": self.analyzer,
            "prompt_version": self.prompt_version,
            "raw_response": self.raw_response,
        }


@dataclass(frozen=True)
class SimilarityCandidate:
    key: str
    summary: str
    status: str
    score: float
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def parse_transcript(path: str | Path) -> Transcript:
    path = Path(path)
    raw = path.read_text(encoding="utf-8")
    lines = raw.splitlines()
    if len(lines) < 3:
        raise TranscriptParseError(f"{path}: transcript is too short")

    header = HEADER_RE.match(lines[0])
    generic_header = GENERIC_HEADER_RE.match(lines[0])
    metadata = META_RE.match(lines[1])
    if not generic_header or not metadata:
        raise TranscriptParseError(f"{path}: invalid call header or metadata")

    turns: list[Turn] = []
    for line_no, line in enumerate(lines, 1):
        match = TURN_RE.match(line)
        if match:
            turns.append(
                Turn(
                    role=match.group("role"),
                    speaker=match.group("speaker").strip(),
                    text=match.group("text").strip(),
                    line_no=line_no,
                )
            )

    if not turns:
        raise TranscriptParseError(f"{path}: no speaker turns found")

    internal = next((turn.speaker for turn in turns if turn.role == "INTERNAL"), "unknown-owner")
    return Transcript(
        call_id=metadata.group("call_id"),
        account=(header.group("account").strip() if header else "Internal BetterBark"),
        call_kind=(header.group("kind").strip() if header else generic_header.group("title").strip()),
        date=metadata.group("date").strip(),
        owner=internal,
        path=path,
        raw=raw,
        turns=tuple(turns),
        sha256=hashlib.sha256(raw.encode("utf-8")).hexdigest(),
    )


def _normalized_exact(value: str) -> str:
    return " ".join(value.strip().split())


def ground_evidence(transcript: Transcript, evidence: Iterable[Evidence]) -> list[Evidence]:
    """Return source-grounded evidence or fail closed.

    The model may get a line number or speaker spelling slightly wrong. The quote
    itself may not be approximate: it must exactly match an EXTERNAL turn after
    whitespace normalization. We correct metadata from the source of truth.
    """

    grounded: list[Evidence] = []
    external_by_text: dict[str, Turn] = {
        _normalized_exact(turn.text): turn for turn in transcript.external_turns
    }
    for item in evidence:
        quote = _normalized_exact(item.quote)
        turn = external_by_text.get(quote)
        grounded_quote = turn.text if turn else ""
        if turn is None and len(item.quote.strip()) >= 20:
            substring_matches = [
                candidate for candidate in transcript.external_turns if item.quote.strip() in candidate.text
            ]
            if len(substring_matches) == 1:
                turn = substring_matches[0]
                grounded_quote = item.quote.strip()
        if turn is None:
            raise ProposalValidationError(
                f"evidence is not an exact EXTERNAL utterance in {transcript.call_id}: {item.quote!r}"
            )
        grounded.append(
            Evidence(
                quote=grounded_quote,
                speaker=turn.speaker,
                line_start=turn.line_no,
                line_end=turn.line_no,
            )
        )
    if not grounded:
        raise ProposalValidationError(f"{transcript.call_id}: proposal has no external evidence")
    return grounded


def validate_and_ground_proposal(transcript: Transcript, proposal: ProposedIssue) -> ProposedIssue:
    if proposal.issue_type not in {"Bug", "Feature"}:
        raise ProposalValidationError(f"unsupported issue type: {proposal.issue_type!r}")
    if not proposal.summary.strip() or len(proposal.summary) > 220:
        raise ProposalValidationError("summary must be 1-220 characters")
    if not 0.0 <= proposal.confidence <= 1.0:
        raise ProposalValidationError("confidence must be between 0 and 1")
    proposal.evidence = ground_evidence(transcript, proposal.evidence)
    return proposal


def stable_hash(*parts: str, length: int = 20) -> str:
    raw = "\x1f".join(parts).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:length]


def slugify(value: str) -> str:
    ascii_value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", "-", ascii_value.lower()).strip("-") or "unknown"


def compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
