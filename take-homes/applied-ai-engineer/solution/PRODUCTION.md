# Production runbook

This repository can run as a single-worker scheduled intake service. Production
mode uses a primary model plus a distinct model fallback and circuit breaker,
Jira Cloud, Slack, a
durable SQLite outbox, JSON logs, a human approval gate, and dead-letter recovery.
It refuses to use the heuristic analyzer or local sink stubs in production.

It is not ready for an unreviewed public launch. The model path and live adapters
must first pass sandbox and canary validation with the target organization's
schemas, permissions, retention rules, and real traffic.

## Required environment

| Variable | Requirement |
| --- | --- |
| `BETTERBARK_ENV` | `production` |
| `BETTERBARK_SINK_MODE` | `live` |
| `OPENAI_API_KEY` | Secret with model access |
| `OPENAI_MODEL` | Calibrated primary model |
| `OPENAI_FALLBACK_MODEL` | Different calibrated model |
| `JIRA_BASE_URL` | Jira Cloud base URL |
| `JIRA_EMAIL`, `JIRA_API_TOKEN` | Least-privilege Jira credentials |
| `JIRA_PROJECT_KEY` | Destination project |
| `SLACK_BOT_TOKEN` | Bot token with `chat:write` |
| `SLACK_INTAKE_CHANNEL_ID` | Fallback channel ID |
| `SLACK_OWNER_IDS_JSON` | JSON map from transcript owner names to Slack user IDs |
| `BETTERBARK_STATE` | Absolute path on an encrypted persistent volume |
| `BETTERBARK_TRANSCRIPTS_DIR` | Read-only directory populated by the upstream exporter |
| `BETTERBARK_EXISTING_ISSUES` | Validated issue snapshot path |

Secrets must come from the platform secret manager, not a checked-in env file.
Run the exporter with atomic writes: write a temporary file, fsync it, then rename
it into the input directory. The state volume contains customer quotes and must
be encrypted, access-controlled, backed up, and covered by a retention policy.

## Build and preflight

From `take-homes/applied-ai-engineer/`:

```bash
docker build -f solution/Dockerfile -t betterbark-intake:VERSION .
docker run --rm --env-file /secure/runtime.env \
  -v betterbark-state:/data/state \
  -v /secure/transcripts:/data/input:ro \
  -v /secure/config:/data/config:ro \
  betterbark-intake:VERSION \
  --state /data/state/state.db preflight --provider openai
```

Pin the base image by digest in the release pipeline, scan the resulting image,
generate an SBOM, sign it, and deploy the immutable image digest. The Kubernetes
template in `deploy/kubernetes.yaml` runs as a non-root user with a read-only root
filesystem, resource limits, no Linux capabilities, and overlapping-run guards.

Create platform resources before applying it:

```bash
kubectl create configmap betterbark-existing-issues \
  --from-file=existing_issues.json=data/existing_issues.json
kubectl create secret generic betterbark-secrets \
  --from-literal=OPENAI_API_KEY=... \
  --from-literal=JIRA_BASE_URL=... \
  --from-literal=JIRA_EMAIL=... \
  --from-literal=JIRA_API_TOKEN=... \
  --from-literal=JIRA_PROJECT_KEY=... \
  --from-literal=SLACK_BOT_TOKEN=... \
  --from-literal=SLACK_INTAKE_CHANNEL_ID=...
```

Replace every `REPLACE_WITH_*` value and the sample image reference before
deployment. Treat the manifest as a starting template, not a one-command release.

## Safe rollout

1. Run all tests and the repeated dev eval.
2. Run the model eval at least five times. Block rollout on any safety-case
   regression and review the worst run, not only the average.
3. Exercise Jira and Slack sandbox projects, including timeout-after-write,
   rate-limit, invalid-schema, permission-denied, and recovery scenarios.
4. Run `scan` against a representative canary batch. It cannot write either sink.
5. Have a reviewer inspect every staged item. Reject incorrect proposals and
   calibrate thresholds before enabling live approval.
6. Start with one dispatcher and a small account allowlist. Increase scope only
   after reviewing action rate, duplicate rate, invalid-proposal rate, fallback
   rate, precision samples, and reviewer disagreement.
7. Back up the state volume and perform a restore drill before broad rollout.

Approval is still explicit:

```bash
python -m solution --state "$BETTERBARK_STATE" list
python -m solution --state "$BETTERBARK_STATE" show REVIEW_ID
python -m solution --state "$BETTERBARK_STATE" approve REVIEW_ID
```

The approve transaction durably enqueues Jira and Slack work before delivery.
Jira creation reconciles a deterministic label before every attempt. Slack sends
a deterministic `client_msg_id` and metadata hash. No automatic model decision
can approve its own output.

The adapters target the official Jira Cloud
[create](https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issues/)
and [search](https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issue-search/)
APIs and [Slack `chat.postMessage`](https://docs.slack.dev/reference/methods/chat.postMessage/).
Revalidate both contracts during dependency and tenant upgrade reviews.

## Operations

Use `health --strict` for alert checks, not container liveness. A dead letter is
degraded service and requires investigation:

```bash
python -m solution --state "$BETTERBARK_STATE" health --strict
python -m solution --state "$BETTERBARK_STATE" status
python -m solution --state "$BETTERBARK_STATE" dead-letters
python -m solution --state "$BETTERBARK_STATE" retry-dead EVENT_KEY
python -m solution --state "$BETTERBARK_STATE" dispatch
```

Alert on any dead letter, scan error or invalid-proposal rate above the configured
limit, unexpected zero-input/zero-output runs, repeated model fallback, review
backlog age, action-rate drift, and duplicate-rate drift. Route JSON logs to the
central log platform and derive metrics by `event`, `level`, `run_id`, analyzer,
fallback status, sink, and outcome. Logs redact secret-bearing fields and common
token formats and never intentionally include transcript text.

## Known launch blockers

- The vendor-model path has not been executed here because no API key was
  provided. Its quality, latency, quota, and fallback behavior are unverified.
- Jira and Slack clients are contract-tested with mocks but not tested against
  the target tenant. Jira field names, issue types, priority names, and app
  permissions vary by tenant.
- The input exporter from the source call platform is outside this take-home.
- SQLite is intentionally a single-worker design. Keep one dispatcher and one
  state volume. Move to PostgreSQL before multi-region or horizontally scaled
  operation.
- There is no automatic data-retention or erasure job and no organization-specific
  PII policy implementation.
- The review surface is an operator CLI/JSON contract, not an authenticated web
  UI. Add the organization's SSO-protected review UI before broad non-technical use.

These blockers require credentials, tenant configuration, security decisions,
and stakeholder acceptance. They cannot be truthfully cleared by local tests.
