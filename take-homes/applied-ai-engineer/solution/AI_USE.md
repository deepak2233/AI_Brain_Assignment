# AI use disclosure

## Tool used

I used OpenAI Codex in ChatGPT Work Mode as the primary implementation agent.

## Work split

I supplied the goal to complete the take-home and, after the first working
version, asked for production hardening: remove nonessential comments, add
explicit fallback behavior, structured logging, safer failure recovery, and
production configuration checks.

I delegated the following work to Codex:

- reading the brief, transcripts, labels, existing issues, and sink stubs;
- proposing the architecture and implementation boundary;
- authoring every file currently submitted under `solution/`;
- writing the unit tests and development evaluation;
- running the tests, repeated dev evaluation, 140-transcript operational scan,
  idempotency checks, and clean-copy integration checks;
- adding the OpenAI-compatible analyzer, Jira and Slack adapters, Dockerfile,
  Kubernetes template, and production runbook; and
- drafting the README, write-up, and this disclosure for my review.

My direct contribution was the task direction and the production-hardening
requirements above. I did not manually author the current Python implementation
before the baseline commit. I should not represent the code as independently
handwritten.

## A concrete override

I did not accept the first working prototype as sufficient for a real deployment.
I asked Codex to add fallback handling, production-level logs, failure recovery,
configuration safeguards, and comment cleanup. That request led to fail-closed
production configuration, visible analyzer fallback and circuit breaking,
bounded outbox retries, dead-letter recovery, secret-redacted JSON logs, and live
adapter contracts.

## Where the tool was wrong

Codex's own validation found several problems in its earlier output:

- the initial offline extractor passed only 6 of 15 labeled calls;
- an early duplicate rule incorrectly matched call 001's dashboard-card bug to
  `PROJ-118` based on weak lexical overlap; and
- the first dispatcher could consume the full retry budget for a failed sink in
  one command invocation.

Codex corrected these issues and added regression tests. I did not personally
discover those three defects, so I am not claiming them as my own overrides.

## Verification and limits

Codex reported the following local results, which I must be able to reproduce and
explain:

- 31 of 31 unit and integration-contract tests passed;
- five heuristic dev-eval runs passed 15 of 15 labeled calls with a stable-case
  rate of 1.0;
- the 140-transcript operational scan completed and a same-version rerun was
  idempotent; and
- approval replay against the supplied Jira and Slack stubs produced no duplicate
  effects.

These results do not validate the OpenAI-backed analyzer, the 125-call holdout, or
the live Jira and Slack tenants. No model API key or live-service credentials
were available. The live adapters were exercised only through mocked contract
tests. SQLite also limits this implementation to a single dispatcher and shared
state volume.

Before submitting, I need to rerun the documented commands myself, inspect the
review queue, and complete a line-by-line code review. Until that is done, I
cannot honestly claim that I can explain every line in the technical deep-dive.

## Commit history

The implementation and hardening work originally existed as one untracked Codex
working tree. Commit `ee5ac93` records that state as an explicitly AI-assisted
baseline. I did not create retroactive commits pretending to show a development
sequence that Git never recorded. Changes after that baseline are committed in
the order they were actually made.

The two Codex sessions were previously estimated at roughly five hours of focused
build and automated verification time. I have not recorded separate independent
review time because that review is not yet complete.
