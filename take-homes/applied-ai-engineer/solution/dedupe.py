"""Deterministic duplicate retrieval and priority policy.

Retrieval is deterministic so reviewers can see why an issue was considered a
duplicate. The model may suggest a key, but the suggestion is never trusted
without validating the key and a minimum evidence overlap.
"""

from __future__ import annotations

import json
import math
import re
import unicodedata
from pathlib import Path
from typing import Any, Iterable

from .domain import ProposedIssue, SimilarityCandidate


STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "been", "but", "by", "can", "could",
    "customer", "do", "does", "for", "from", "has", "have", "i", "if", "in", "into",
    "is", "it", "its", "me", "more", "not", "of", "on", "or", "our", "some", "that",
    "the", "their", "them", "then", "there", "this", "to", "up", "us", "user", "users",
    "was", "we", "were", "when", "which", "while", "with", "would", "you", "your",
    "issue", "product", "request", "report", "reports", "member", "members",
}

PHRASE_NORMALIZATION = {
    "sign in": " login ",
    "log in": " login ",
    "logged in": " login ",
    "azure active directory": " azuread ",
    "azure ad": " azuread ",
    "password-reset": " passwordreset ",
    "password reset": " passwordreset ",
    "force quit": " forcequit ",
    "force-quit": " forcequit ",
    "white screen": " whitescreen ",
    "crash on launch": " launchcrash ",
    "crashes on launch": " launchcrash ",
    "calendar invite": " calendarinvite ",
    "time zone": " timezone ",
    "idempotency key": " idempotencykey ",
    "active members": " activemembers ",
    "hours ahead": " timezone ",
    "hour ahead": " timezone ",
}

IMPORTANT = {
    "android", "ios", "okta", "azuread", "saml", "sso", "outlook", "calendarinvite",
    "webhook", "idempotencykey", "timezone", "passwordreset", "launchcrash", "whitescreen",
    "dashboard", "search", "stale", "redirect", "loop", "email", "export", "api", "scim",
    "photo", "upload", "session", "deactivate", "duplicate", "timestamp", "notification",
}

SYMPTOMS = {
    "crash", "wrong", "missing", "delay", "stale", "duplicate", "blank", "whitescreen",
    "truncate", "freeze", "drop", "loop", "expire", "fail", "logout", "404", "reset",
}

ENTITY_GROUPS = (
    {"okta", "azuread", "saml"},
    {"android", "ios"},
    {"outlook", "googlecalendar"},
)


def load_existing_issues(path: str | Path) -> list[dict[str, Any]]:
    with Path(path).open(encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise ValueError("existing_issues.json must contain an array of objects")
    required = {"key", "summary", "status"}
    keys: set[str] = set()
    for index, item in enumerate(value):
        missing = required - item.keys()
        if missing:
            raise ValueError(
                f"existing issue at index {index} is missing: {', '.join(sorted(missing))}"
            )
        key = item["key"]
        if not isinstance(key, str) or not key.strip():
            raise ValueError(f"existing issue at index {index} has an invalid key")
        if key in keys:
            raise ValueError(f"existing issue key is duplicated: {key}")
        if not isinstance(item["summary"], str) or not item["summary"].strip():
            raise ValueError(f"existing issue {key} has an invalid summary")
        if not isinstance(item["status"], str) or not item["status"].strip():
            raise ValueError(f"existing issue {key} has an invalid status")
        keys.add(key)
    return value


def _ascii(value: str) -> str:
    return unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii").lower()


def tokenize(value: str) -> set[str]:
    normalized = f" {_ascii(value)} "
    for phrase, replacement in PHRASE_NORMALIZATION.items():
        normalized = normalized.replace(phrase, replacement)
    tokens: set[str] = set()
    for raw in re.findall(r"[a-z0-9]+", normalized):
        if raw in STOPWORDS or len(raw) < 2:
            continue
        token = raw
        for suffix in ("ingly", "edly", "ation", "ments", "ment", "ing", "ies", "ed", "es", "s"):
            if token.endswith(suffix) and len(token) - len(suffix) >= 4:
                token = token[: -len(suffix)] + ("y" if suffix == "ies" else "")
                break
        tokens.add(token)
    return tokens


def _weight(token: str) -> float:
    if token in IMPORTANT:
        return 3.0
    if token in SYMPTOMS:
        return 2.25
    if token.isdigit():
        return 0.35
    return 1.0


def weighted_jaccard(left: Iterable[str], right: Iterable[str]) -> float:
    left_set, right_set = set(left), set(right)
    union = left_set | right_set
    if not union:
        return 0.0
    intersection = left_set & right_set
    return sum(_weight(token) for token in intersection) / sum(_weight(token) for token in union)


def _entity_conflict(left: set[str], right: set[str]) -> str | None:
    for group in ENTITY_GROUPS:
        left_values = left & group
        right_values = right & group
        if left_values and right_values and left_values.isdisjoint(right_values):
            return f"scope conflict: {sorted(left_values)} vs {sorted(right_values)}"
    return None


def similarity(left_text: str, right_text: str) -> tuple[float, str]:
    left_ascii, right_ascii = _ascii(left_text), _ascii(right_text)
    if re.search(r"(?:not|isn.t|distinct from|different (?:from|idp)).{0,35}\bokta\b", left_ascii) and "okta" in right_ascii:
        return 0.0, "explicitly distinguished from the Okta-scoped issue"
    left, right = tokenize(left_text), tokenize(right_text)
    audit_api = {"audit", "siem", "export"}
    if "webhook" in left and (right & audit_api) and "webhook" not in right:
        return 0.0, "integration-shape conflict: webhook event vs audit export API"
    if "webhook" in right and (left & audit_api) and "webhook" not in left:
        return 0.0, "integration-shape conflict: audit export API vs webhook event"
    conflict = _entity_conflict(left, right)
    if conflict:
        return 0.0, conflict
    overlap = left & right
    score = weighted_jaccard(left, right)
    important_overlap = overlap & IMPORTANT
    symptom_overlap = overlap & SYMPTOMS
    if important_overlap:
        score += min(0.12, 0.04 * len(important_overlap))
    if symptom_overlap:
        score += min(0.08, 0.04 * len(symptom_overlap))
    score = min(1.0, score)
    reason = (
        f"weighted token overlap; important={sorted(important_overlap)}; "
        f"symptoms={sorted(symptom_overlap)}"
    )
    return score, reason


def proposal_text(proposal: ProposedIssue) -> str:
    evidence = " ".join(item.quote for item in proposal.evidence)
    return f"{proposal.issue_type} {proposal.product_area} {proposal.summary} {proposal.description} {evidence}"


def existing_issue_text(issue: dict[str, Any]) -> str:
    return f"{issue.get('type', '')} {issue.get('summary', '')} {issue.get('description', '')}"


def rank_existing(
    proposal: ProposedIssue, existing_issues: list[dict[str, Any]], *, limit: int = 3
) -> list[SimilarityCandidate]:
    proposal_segments = [proposal.summary, proposal.description] + [item.quote for item in proposal.evidence]
    ranked: list[SimilarityCandidate] = []
    for issue in existing_issues:
        issue_segments = [str(issue.get("summary", "")), str(issue.get("description", "")), existing_issue_text(issue)]
        comparisons = [
            (*similarity(left, right), left, right)
            for left in proposal_segments
            for right in issue_segments
            if left and right
        ]
        score, reason, _, _ = max(comparisons, key=lambda item: item[0])
        if proposal.duplicate_target == issue.get("key"):
            score = min(1.0, score + 0.18)
            reason += "; analyzer suggested this validated key"
        ranked.append(
            SimilarityCandidate(
                key=str(issue.get("key")),
                summary=str(issue.get("summary", "")),
                status=str(issue.get("status", "Unknown")),
                score=round(score, 4),
                reason=reason,
            )
        )
    return sorted(ranked, key=lambda item: (-item.score, item.key))[:limit]


def choose_existing_match(
    proposal: ProposedIssue,
    existing_issues: list[dict[str, Any]],
    *,
    threshold: float = 0.24,
) -> tuple[SimilarityCandidate | None, list[SimilarityCandidate]]:
    ranked = rank_existing(proposal, existing_issues)
    if not ranked:
        return None, []
    keys = {str(item.get("key")) for item in existing_issues}
    if proposal.duplicate_target and proposal.duplicate_target not in keys:
        proposal.duplicate_target = None
        proposal.duplicate_rationale = "Analyzer suggested an unknown key; suggestion discarded"
    top = ranked[0]
    # A model suggestion is only a hint. It still needs non-trivial lexical evidence.
    suggested_floor = 0.27 if proposal.duplicate_target == top.key else threshold
    top_issue = next((item for item in existing_issues if str(item.get("key")) == top.key), None)
    strong_overlap: set[str] = set()
    if top_issue:
        strong_overlap = (
            tokenize(proposal_text(proposal)) & tokenize(existing_issue_text(top_issue))
        ) & (IMPORTANT | SYMPTOMS)
    enough_evidence = (
        len(strong_overlap) >= 2
        or top.score >= 0.32
        or proposal.duplicate_target == top.key
    )
    return (top if top.score >= suggested_floor and enough_evidence else None), ranked


def rank_review_items(
    proposal: ProposedIssue, review_items: list[dict[str, Any]], *, limit: int = 3
) -> list[SimilarityCandidate]:
    ranked: list[SimilarityCandidate] = []
    left_segments = [proposal.summary, proposal.description] + [evidence.quote for evidence in proposal.evidence]
    for item in review_items:
        if item.get("action") != "file-new" or item.get("status") == "rejected":
            continue
        right = " ".join(
            str(item.get(field, ""))
            for field in ("issue_type", "product_area", "summary", "description")
        )
        right_segments = [str(item.get("summary", "")), str(item.get("description", "")), right]
        comparisons = [
            similarity(left, right_value)
            for left in left_segments
            for right_value in right_segments
            if left and right_value
        ]
        score, reason = max(comparisons, key=lambda value: value[0])
        ranked.append(
            SimilarityCandidate(
                key=str(item["id"]),
                summary=str(item["summary"]),
                status=str(item["status"]),
                score=round(score, 4),
                reason=reason,
            )
        )
    return sorted(ranked, key=lambda item: (-item.score, item.key))[:limit]


def choose_review_cluster(
    proposal: ProposedIssue,
    review_items: list[dict[str, Any]],
    *,
    threshold: float = 0.18,
) -> tuple[SimilarityCandidate | None, list[SimilarityCandidate]]:
    ranked = rank_review_items(proposal, review_items)
    if not ranked:
        return None, []
    top_item = next((item for item in review_items if str(item.get("id")) == ranked[0].key), None)
    same_area = bool(top_item and top_item.get("product_area") == proposal.product_area)
    return (ranked[0] if ranked[0].score >= threshold and same_area else None), ranked


def priority_policy(proposal: ProposedIssue) -> tuple[str, str, str]:
    """Map observable impact to a stable severity and Jira priority."""

    text = _ascii(proposal_text(proposal))
    if re.search(r"\b(?:typo|misspell|cosmetic|font|color|colour)\b", text):
        return "S4", "P4", "Cosmetic or trivial impact"
    if re.search(r"\b(?:data exposure|privacy|security breach|cross[- ]tenant|all users blocked)\b", text):
        return "S1", "P1", "Security/privacy risk or broad outage"
    high_impact = re.search(
        r"\b(?:audit|soc\s*2|finance|renewal|blocking|every notification|all users|wrong number|"
        r"cannot login|can.t login|locked out|data loss)\b",
        text,
    )
    scale = re.search(r"\b(?:every|all|\d{2,}|daily|weekly|consisten|reproduc)\b", text)
    if high_impact or (scale and proposal.issue_type == "Bug" and proposal.confidence >= 0.8):
        return "S2", "P2", "Material business/core-workflow impact"
    return "S3", "P3", "Real but bounded impact or workaround available"


def confidence_band(value: float) -> str:
    if not math.isfinite(value):
        return "invalid"
    if value >= 0.85:
        return "high"
    if value >= 0.65:
        return "medium"
    return "low"
