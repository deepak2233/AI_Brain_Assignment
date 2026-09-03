# The June Tapes - solution write-up

## What I built

I built a Python 3.11, standard-library-only intake service with four explicit
stages: parse and hash each call; semantically extract genuine customer issues;
retrieve existing or in-batch duplicates; and stage a compact human-review item.
`scan` cannot call Jira or Slack. An explicit `approve` transaction creates a
durable outbox, then the dispatcher calls either the supplied stubs or configured
Jira Cloud and Slack adapters. Reviewers see the
proposed action, ticket and notification previews, priority rationale, nearest
duplicate candidates, and verbatim external-speaker evidence linked to source
lines. Multiple customer calls can become one ticket with several sources.

The semantic analyzer is swappable. The intended path is one low-temperature,
structured model call per transcript with bounded retries and a distinct model
fallback. Fallback selection is logged and stored. A conservative heuristic
baseline keeps the repository runnable without credentials and provides a
deterministic smoke test. Production mode rejects heuristic analysis and
heuristic fallback.

## Where AI belongs, and where it does not

AI decides whether conversational language represents a genuine Bug or Feature
and drafts the concise problem statement. That is where paraphrase, retraction,
secondhand context, and intent make rules brittle. The transcript is sent as
untrusted JSON under a prompt that explicitly treats caller instructions as
data. The model can suggest an existing Jira key, but that suggestion is only a
hint.

Everything with control-plane consequences is deterministic: speaker parsing,
content hashes, exact evidence provenance, schema checks, allowed issue types,
priority policy, duplicate-key existence, similarity traces, review state,
idempotency keys, sink order, retries, and audit logs. A proposal with no exact
external quote fails closed. Internal-only calls skip the model. This prevented
the joke instruction in call 005 from becoming a ticket or Slack message while
retaining the genuine webhook report later in the same call.

## Hardest engineering problem: safe effects across systems

The hard part was not the prompt. It was preserving one logical approval across
two naive, non-transactional sinks when a process can fail between "remote write
succeeded" and "local state recorded success." Exactly-once delivery is
impossible if the receiver offers neither transactions nor idempotency. For the
provided stubs, every effect has a deterministic key. The dispatcher holds a
file lock, reconciles the sink log for that key, writes only if absent, and then
marks the SQLite outbox event delivered. If it crashes after append, replay
finds the existing record instead of appending again. Slack depends on the Jira
event and receives the returned Jira key. The live Jira adapter searches a
deterministic label before creation and stores an issue property; the Slack
adapter uses a deterministic `client_msg_id` and metadata hash. A failed event is
attempted once per dispatcher invocation, becomes a visible dead letter after a
bounded budget, and requires explicit operator requeue. I still do not claim
exactly-once against a remote API that cannot guarantee it.

Each transcript is processed in its own database transaction. A model or parser
failure rolls back only that call, is recorded with its hash and error, and the
run continues. Successful hashes are keyed by analyzer and prompt version, so a
same-version rerun skips them; a changed prompt deliberately creates a new
analysis run. New sources attaching to a pending issue update that single review
item. Sources arriving after approval require a new corroboration review rather
than bypassing the gate.

## De-duplication and review policy

I use deterministic weighted-token retrieval with explicit scope guards for
identity provider, mobile platform, calendar client, and integration shape.
High similarity attaches a source to a known issue or pending cluster; ambiguous
neighbors stay visible to the reviewer. This matters in call 010: an Azure AD
post-password-change redirect loop must not be folded into an Okta early-session-
expiry issue merely because both contain "SSO" and "login." A shipped match is
suppressed as enablement, not filed again. The supplied Jira stub has no update
operation, so approved existing-ticket corroboration is durably recorded in the
outbox/state and notified in Slack without calling `create_issue`; production
would add a Jira comment or affected-account link.

Priority is policy-driven, not copied from customer urgency. Security/privacy or
broad outage is P1, material business/core-flow impact P2, bounded impact or a
workaround P3, and cosmetic/copy defects P4. That is why "BetterBrak" stays P4
despite the customer's joking P0 framing.

## Eval, validation, and observability

The dev eval is issue-level, not call-level sentiment. A call passes only when
the actionable count is exact; new issues have the correct type and semantic
match; corroborations hit the correct existing key or in-batch cluster; the typo
is P4; all evidence is externally grounded; and injection text produces no
effect. The credential-free baseline scores **15/15 calls, precision 1.00,
recall 1.00, F1 1.00** on calls 001-015. Five independent baseline runs have a
1.00 stable-case rate. The idempotency check scans the same 15 files again and
gets 15 skips with unchanged review/source counts. Unit tests also inject a
per-call analyzer failure and replay a sink write after an uncertain response.
The expanded 31-test suite covers malformed and oversized files, duplicate content,
decision replay, fallback routing, config fail-closed rules, secret redaction,
legacy state migration, dead-letter recovery, and mocked live API contracts.

Those numbers are development results, not a holdout claim. The labels are
small, summary matching is lexical, and the baseline was tuned against them.
With the model provider I would require at least five independent runs, report
the worst-run precision/recall and the fraction of cases passing every run, and
block rollout on any safety-case regression. I could not execute the vendor
model path here without an API key, so I do not invent model reliability
numbers. What may still slip through: semantically related but distinct issues,
subtle account-service requests framed as features, or a genuine report whose
only evidence is secondhand. The stored raw decision, ignored reasons, exact
quote, duplicate scores, prompt version, model name, latency, and transcript hash
let an engineer distinguish a model miss from a disputed label.

Every run emits leveled JSON events with service, environment, version, run,
model/fallback, latency, dedupe, and sink outcome fields. Secret-bearing fields
and common token shapes are redacted; transcript text is not intentionally
logged. `status` and `health --strict` expose failed inputs, invalid proposals,
review state, blocked dependencies, and dead letters. Production should alert on
unexpected zero-output runs, error/invalid/fallback spikes, review backlog age,
action-rate drift, duplicate-rate drift, and any dead letter.

## AI-tool disclosure

I used OpenAI Codex to inspect the brief and repository, draft much of the Python
implementation and tests, and challenge failure modes. I retained the design
decisions, ran the code, inspected the generated queue, and iterated from
evidence rather than accepting generated code as correct. One concrete override:
an early duplicate rule accepted call 001's dashboard-card bug as PROJ-118 because
both contained "dashboard." I rejected that output, required multiple strong
behavioral anchors (or a materially higher score), and added a regression test.
The first offline extractor also scored only 6/15; I did not report that green by
weakening action-count checks. I corrected provenance, scope conflicts, and
clustering until the agreed contract passed. Codex did not supply credentials,
run the external model, or submit the PR. I can explain and modify every part of
the final code.

## Deliberate omissions and another day

I deliberately omitted an authenticated web UI, the source-call-platform
exporter, concurrent workers/PostgreSQL, real credentials, and organization-
specific retention/erasure policy. Automatic filing remains intentionally out of
scope: the human gate is a safety property. Before launch I would run and
calibrate the model across repeated trials, test the live adapters against tenant
sandboxes, add Jira corroboration comments and Slack Block Kit, and export
metrics/traces to the production stack. The container, Kubernetes template, safe
rollout, recovery procedure, and honest launch blockers are in `PRODUCTION.md`.
Approximate focused build and verification time: five hours, AI-assisted.
