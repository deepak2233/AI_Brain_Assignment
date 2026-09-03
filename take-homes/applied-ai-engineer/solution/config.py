from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


class ConfigurationError(ValueError):
    pass


def _boolean(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ConfigurationError(f"{name} must be true or false")


def _integer(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise ConfigurationError(f"{name} must be between {minimum} and {maximum}")
    return value


def _number(name: str, default: float, minimum: float, maximum: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be numeric") from exc
    if not minimum <= value <= maximum:
        raise ConfigurationError(f"{name} must be between {minimum} and {maximum}")
    return value


def _mapping(name: str, default: dict[str, str] | None = None) -> dict[str, str]:
    raw = os.getenv(name)
    if not raw:
        return dict(default or {})
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigurationError(f"{name} must be a JSON object") from exc
    if not isinstance(value, dict) or any(
        not isinstance(key, str) or not isinstance(item, str) for key, item in value.items()
    ):
        raise ConfigurationError(f"{name} must map strings to strings")
    return value


@dataclass(frozen=True)
class RuntimeConfig:
    environment: str
    log_level: str
    service_name: str
    service_version: str
    sink_mode: str
    max_outbox_attempts: int
    max_transcript_bytes: int
    allow_heuristic_fallback: bool
    transcript_dir: Path
    existing_issues_path: Path

    @classmethod
    def from_env(cls, exercise_root: Path) -> "RuntimeConfig":
        environment = os.getenv("BETTERBARK_ENV", "development").strip().lower()
        if environment not in {"development", "test", "production"}:
            raise ConfigurationError(
                "BETTERBARK_ENV must be development, test, or production"
            )
        log_level = os.getenv("LOG_LEVEL", "INFO").strip().upper()
        if log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ConfigurationError("LOG_LEVEL is not valid")
        sink_mode = os.getenv("BETTERBARK_SINK_MODE", "stub").strip().lower()
        if sink_mode not in {"stub", "live"}:
            raise ConfigurationError("BETTERBARK_SINK_MODE must be stub or live")
        return cls(
            environment=environment,
            log_level=log_level,
            service_name=os.getenv("SERVICE_NAME", "betterbark-intake").strip()
            or "betterbark-intake",
            service_version=os.getenv("SERVICE_VERSION", "dev").strip() or "dev",
            sink_mode=sink_mode,
            max_outbox_attempts=_integer("OUTBOX_MAX_ATTEMPTS", 5, 1, 20),
            max_transcript_bytes=_integer(
                "MAX_TRANSCRIPT_BYTES", 2_000_000, 1024, 20_000_000
            ),
            allow_heuristic_fallback=_boolean("ALLOW_HEURISTIC_FALLBACK", False),
            transcript_dir=Path(
                os.getenv("BETTERBARK_TRANSCRIPTS_DIR", str(exercise_root / "transcripts"))
            ),
            existing_issues_path=Path(
                os.getenv(
                    "BETTERBARK_EXISTING_ISSUES",
                    str(exercise_root / "data" / "existing_issues.json"),
                )
            ),
        )

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    def validate_provider(self, provider: str) -> None:
        if not self.is_production:
            return
        if provider == "heuristic":
            raise ConfigurationError("the heuristic analyzer is disabled in production")
        if not os.getenv("OPENAI_API_KEY"):
            raise ConfigurationError("OPENAI_API_KEY is required in production")
        primary = os.getenv("OPENAI_MODEL", "gpt-4.1-mini").strip()
        fallback = os.getenv("OPENAI_FALLBACK_MODEL", "").strip()
        if not fallback:
            raise ConfigurationError("OPENAI_FALLBACK_MODEL is required in production")
        if fallback == primary:
            raise ConfigurationError("OPENAI_FALLBACK_MODEL must differ from OPENAI_MODEL")
        if self.allow_heuristic_fallback:
            raise ConfigurationError("heuristic fallback is disabled in production")
        base_url = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
        parsed_base_url = urlparse(base_url)
        if parsed_base_url.scheme != "https" or not parsed_base_url.hostname:
            raise ConfigurationError("OPENAI_BASE_URL must use HTTPS in production")
        _integer("OPENAI_MAX_ATTEMPTS", 3, 1, 10)
        _number("OPENAI_TIMEOUT_SECONDS", 75, 1, 300)
        _integer("MODEL_CIRCUIT_FAILURE_THRESHOLD", 3, 1, 50)
        _number("MODEL_CIRCUIT_COOLDOWN_SECONDS", 60, 1, 3600)

    def validate_live_sinks(self) -> None:
        if self.sink_mode != "live":
            if self.is_production:
                raise ConfigurationError(
                    "BETTERBARK_SINK_MODE=live is required for production delivery"
                )
            return
        required = (
            "JIRA_BASE_URL",
            "JIRA_EMAIL",
            "JIRA_API_TOKEN",
            "JIRA_PROJECT_KEY",
            "SLACK_BOT_TOKEN",
            "SLACK_INTAKE_CHANNEL_ID",
        )
        missing = [name for name in required if not os.getenv(name)]
        if missing:
            raise ConfigurationError(
                "missing live sink configuration: " + ", ".join(sorted(missing))
            )
        base_url = os.environ["JIRA_BASE_URL"]
        parsed_base_url = urlparse(base_url)
        if parsed_base_url.scheme != "https" or not parsed_base_url.hostname:
            raise ConfigurationError("JIRA_BASE_URL must use HTTPS")
        project = os.environ["JIRA_PROJECT_KEY"]
        if not re.fullmatch(r"[A-Z][A-Z0-9_]{1,19}", project):
            raise ConfigurationError("JIRA_PROJECT_KEY has an invalid format")
        intake_channel = os.environ["SLACK_INTAKE_CHANNEL_ID"]
        if not re.fullmatch(r"[CDGU][A-Z0-9]+", intake_channel):
            raise ConfigurationError("SLACK_INTAKE_CHANNEL_ID must be a Slack ID")
        owners = _mapping("SLACK_OWNER_IDS_JSON")
        invalid_owners = [
            owner
            for owner, slack_id in owners.items()
            if not re.fullmatch(r"[UW][A-Z0-9]+", slack_id)
        ]
        if invalid_owners:
            raise ConfigurationError(
                "SLACK_OWNER_IDS_JSON contains invalid IDs for: "
                + ", ".join(sorted(invalid_owners))
            )
        _mapping(
            "JIRA_PRIORITY_NAMES_JSON",
            {"P1": "Highest", "P2": "High", "P3": "Medium", "P4": "Low"},
        )
        _integer("SINK_HTTP_MAX_ATTEMPTS", 3, 1, 10)
        _number("SINK_HTTP_TIMEOUT_SECONDS", 20, 1, 120)

    def preflight(self, state_path: Path, provider: str) -> dict[str, Any]:
        self.validate_provider(provider)
        self.validate_live_sinks()
        if self.is_production and not state_path.is_absolute():
            raise ConfigurationError("production state path must be absolute")
        if not self.transcript_dir.is_dir():
            raise ConfigurationError(f"transcript directory does not exist: {self.transcript_dir}")
        if not self.existing_issues_path.is_file():
            raise ConfigurationError(
                f"existing issue file does not exist: {self.existing_issues_path}"
            )
        return {
            "environment": self.environment,
            "provider": provider,
            "sink_mode": self.sink_mode,
            "state_path": str(state_path),
            "transcript_dir": str(self.transcript_dir),
            "existing_issues_path": str(self.existing_issues_path),
            "model_fallback_configured": bool(os.getenv("OPENAI_FALLBACK_MODEL")),
            "heuristic_fallback_enabled": self.allow_heuristic_fallback,
            "max_outbox_attempts": self.max_outbox_attempts,
            "max_transcript_bytes": self.max_transcript_bytes,
        }


def slack_owner_ids() -> dict[str, str]:
    return _mapping("SLACK_OWNER_IDS_JSON")


def jira_priority_names() -> dict[str, str]:
    return _mapping(
        "JIRA_PRIORITY_NAMES_JSON",
        {"P1": "Highest", "P2": "High", "P3": "Medium", "P4": "Low"},
    )
