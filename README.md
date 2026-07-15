# BetterBark signal intake

Human-gated product intake for customer-call transcripts. `scan` only stages a
review queue. Jira and Slack stubs are called only after an explicit `approve`.

## Quick start

From `take-homes/applied-ai-engineer/`:

```bash
# Credential-free smoke test over the labeled dev calls
python3 -m solution --state /tmp/betterbark.db scan --provider heuristic \
  transcripts/call-00{1,2,3,4,5,6,7,8,9}.md \
  transcripts/call-01{0,1,2,3,4,5}.md

python3 -m solution --state /tmp/betterbark.db list
python3 -m solution --state /tmp/betterbark.db show <review-id-or-prefix>

# This is the only command that can write to the supplied sinks.
python3 -m solution --state /tmp/betterbark.db approve <review-id-or-prefix>
```

For the intended semantic analyzer:

```bash
export OPENAI_API_KEY=...
export OPENAI_MODEL=gpt-4.1-mini  # override as needed
python3 -m solution --state /tmp/betterbark.db scan --provider openai transcripts/call-*.md
```

`--provider auto` uses the model when `OPENAI_API_KEY` is set and otherwise uses
the conservative, credential-free baseline. The baseline makes the workflow
fully runnable; it is not presented as a substitute for model judgment on the
unlabeled holdout.

## Review and operations

```bash
python3 -m solution --state /tmp/betterbark.db list --status all
python3 -m solution --state /tmp/betterbark.db reject <id> --reason "not actionable"
python3 -m solution --state /tmp/betterbark.db dispatch
python3 -m solution --state /tmp/betterbark.db status
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
worst run, not only the best run.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `BETTERBARK_STATE` | `solution/.runtime/state.db` | Durable SQLite state |
| `OPENAI_API_KEY` | none | Enables the model analyzer |
| `OPENAI_MODEL` | `gpt-4.1-mini` | Model name |
| `OPENAI_BASE_URL` | `https://api.openai.com/v1` | OpenAI-compatible endpoint |
| `OPENAI_TIMEOUT_SECONDS` | `75` | Per-attempt request timeout |

No secrets are written to state or logs.

