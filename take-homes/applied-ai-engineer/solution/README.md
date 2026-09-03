# BetterBark signal intake

Human-gated product intake for customer-call transcripts. `scan` only stages a
review queue. Configured Jira and Slack sinks run only after explicit approval.

The exact human-versus-tool work split and verification limits are documented in
[AI_USE.md](AI_USE.md).

## Quick start

From `take-homes/applied-ai-engineer/`:

```bash
python3 -m solution --state /tmp/betterbark.db scan --provider heuristic \
  transcripts/call-00{1,2,3,4,5,6,7,8,9}.md \
  transcripts/call-01{0,1,2,3,4,5}.md

python3 -m solution --state /tmp/betterbark.db list
python3 -m solution --state /tmp/betterbark.db show <review-id-or-prefix>

python3 -m solution --state /tmp/betterbark.db approve <review-id-or-prefix>
```

For the intended semantic analyzer:

```bash
export OPENAI_API_KEY=...
export OPENAI_MODEL=gpt-4.1-mini
export OPENAI_FALLBACK_MODEL=<different-model>
python3 -m solution --state /tmp/betterbark.db scan --provider openai transcripts/call-*.md
```

`--provider auto` uses the model when `OPENAI_API_KEY` is set and otherwise uses
the conservative, credential-free baseline. The baseline makes the workflow
fully runnable; it is not presented as a substitute for model judgment on the
unlabeled holdout. A configured model fallback is explicit in the analysis record
and JSON logs. Heuristic fallback requires `ALLOW_HEURISTIC_FALLBACK=true` and is
rejected in production.

## Review and operations

```bash
python3 -m solution --state /tmp/betterbark.db list --status all
python3 -m solution --state /tmp/betterbark.db reject <id> --reason "not actionable"
python3 -m solution --state /tmp/betterbark.db dispatch
python3 -m solution --state /tmp/betterbark.db dead-letters
python3 -m solution --state /tmp/betterbark.db retry-dead <event-key>
python3 -m solution --state /tmp/betterbark.db status
python3 -m solution --state /tmp/betterbark.db health --strict
```

The review view includes the proposed action, Jira and Slack payload previews,
priority rationale, exact external-speaker evidence with source lines, every
corroborating call, confidence, and nearest duplicate candidates. A duplicate
approval records corroboration and posts the owner notification; it never calls
the Jira create stub.

## Eval and tests

```bash
python3 -m solution --quiet-logs eval --provider heuristic --runs 5
python3 -m unittest discover -s solution/tests -v
```

The eval requires exact actionable counts per call, correct type/target,
externally grounded evidence, P4 handling for the trivial typo, and no action
from the prompt injection. It also scans the same inputs twice and asserts that
review-item and source counts do not change. With a non-deterministic provider,
`--runs N` creates independent states and reports per-case stability and the
worst run, not only the best run. The 31-test suite covers malformed and
oversized inputs, duplicate content, replayed decisions, fallback routing, secret
redaction, state migration, dead-letter recovery, and mocked live API contracts.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `BETTERBARK_STATE` | `solution/.runtime/state.db` | Durable SQLite state |
| `BETTERBARK_ENV` | `development` | Runtime safety profile |
| `BETTERBARK_SINK_MODE` | `stub` | Local stubs or `live` Jira/Slack |
| `BETTERBARK_TRANSCRIPTS_DIR` | `transcripts/` | Scheduled input directory |
| `BETTERBARK_EXISTING_ISSUES` | `data/existing_issues.json` | Dedupe snapshot |
| `OPENAI_API_KEY` | none | Enables the model analyzer |
| `OPENAI_MODEL` | `gpt-4.1-mini` | Model name |
| `OPENAI_FALLBACK_MODEL` | none | Distinct fallback; required in production |
| `OPENAI_BASE_URL` | `https://api.openai.com/v1` | OpenAI-compatible endpoint |
| `OPENAI_TIMEOUT_SECONDS` | `75` | Per-attempt request timeout |
| `MODEL_CIRCUIT_FAILURE_THRESHOLD` | `3` | Primary failures before temporary bypass |
| `MODEL_CIRCUIT_COOLDOWN_SECONDS` | `60` | Primary-model retry cooldown |
| `OUTBOX_MAX_ATTEMPTS` | `5` | Attempts before dead-lettering |
| `MAX_TRANSCRIPT_BYTES` | `2000000` | Per-file input limit |
| `LOG_LEVEL` | `INFO` | Minimum JSON log level |

Run `python3 -m solution ... preflight --provider openai` before deployment.
Production mode rejects a missing model fallback, heuristic analysis, local stubs,
and incomplete live credentials. Jira and Slack variables, container deployment,
rollout checks, alerts, recovery, and remaining blockers are in
[PRODUCTION.md](PRODUCTION.md).

Secret-bearing log fields and common token formats are redacted. Transcript text
is not intentionally written to logs.
