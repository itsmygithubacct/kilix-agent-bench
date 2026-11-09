# Execution, recovery and evidence

Reference for [kilix-agent-bench](../README.md). This continues the
"Freeze, plan and execute" section of the README.

## Execution

Execution adds `--execute --readiness /path/to/receipt.json`. The trusted
operator supplies a `kilix-agent-bench/execution-readiness-v1` receipt naming
the matching `protocol_sha256`, `qualified_routes` pairs, and evidence for each
configuration. Each configuration proof must contain its
`configuration_sha256` and true `model_accepted`, `settings_verified`,
`usage_complete` and `vm_tool_verified` fields. Two slots additionally require
`two_slots_verified: true`. These are declarations backed by reviewed live
qualification evidence, not a substitute for running that qualification.

Self-hosted execution additionally selects `--configuration ID --endpoint
/path/to/endpoint.json`. The endpoint records `url`, `served_model`, the exact
`configuration_sha256`, optional `key_env`, and `runtime` with a container
`image` digest, `repository_revision`, `precision`, `context_tokens` and
`launch_argv` identifying parser/template settings. HTTPS or a loopback tunnel
is required; redirects and mismatched served-model identities are rejected.
Credentials are referenced by environment variable, never embedded in the URL.
The harness does not create a GPU rental or start an inference server.

Every scored attempt creates and provisions a fresh guest. At most two VM
slots are shared across both tracks and runner processes. A per-track lock
prevents duplicate concurrent batches. Completed task failures are retained
without automatic retries; interrupted or infrastructure-invalid attempts get
new attempt IDs on resume. Logical IDs bind configuration settings as well as
task, route, seed and protocol. Extending a one-round manifest to five rounds
therefore preserves its completed first-round identities.

```sh
kilix-agent-bench recover-attempt /path/to/interrupted-attempt
kilix-agent-bench dataset report
kilix-agent-bench timings /path/to/attempt/timings.jsonl
```

Resume reconciles finalized attempts before scheduling, including crashes
between evidence finalization and progress recording. Recovery only destroys
the VM explicitly recorded in that store's attempt. Already-finalized evidence
remains immutable; later cleanup produces a separate recovery receipt.

## Evidence and measurement

Each attempt retains the exact prompt and configuration, native client events,
raw self-hosted requests/responses, normalized usage, guest command arguments,
bounded outputs and truncation flags, execution audit, independent before/after
observations, scores, timings and cleanup status. A SHA-256 inventory covers all
retained evidence. Ingestion verifies that inventory and deduplicates attempt
IDs. Mutable native-client account state is removed before finalization.

Messages clients retain their saved transcript before cleanup. Its finalized
per-response usage completes early streamed block counts, with message-ID
deduplication and reconciliation of input, cache and output totals against the
session result. Resolved effort comes from response metadata; launch flags and
text inside messages are not evidence of the effort that ran. Grok retains a
small session metadata record and keeps client model names separate from
provider ledger names. A provider alias still requires qualification.

Native controllers receive a fresh isolated home at their normal home path,
read-only credentials, and only the guest command tool. Host files and SSH keys
are outside that namespace. Provider networking remains available. These are
controlled fixtures: command admission and execution auditing do not prove
adversarial isolation against arbitrary interpreter code or a compromised
controller. Live model/effort acceptance and tool behavior require separate
qualification for every selected client configuration.

The primary report is controller tokens per verified success within a
configuration/task/route, including tokens spent on valid failures. Input totals
include cached context; output totals include reasoning where the provider
reports that subset. Neither subset is added twice. Local helper inference
tokens are excluded, while its output consumed by the controller contributes
to subsequent controller input. Missing, conflicting or unreconciled usage
cannot produce a savings score. Zero successes cannot produce one either.
Native clients may expose session aggregates rather than individual responses;
the normalization record states the available granularity.

Limits cover tool calls, command duration, output bytes, controller turns and
session wall time. Token budgets are checked between observable responses, so
an in-flight response can overshoot. Native output-token caps are not assumed
when a client cannot expose one. Self-hosted cancellation takes effect between
requests, with a bounded in-flight timeout. UTC timestamps and monotonic phase
durations include queueing, provisioning, model sessions or requests, tools,
scoring and teardown. Nested spans and concurrent trials are never summed into
an invented batch duration. Timing informs future capacity estimates rather
than serving as a route-efficiency ranking.
