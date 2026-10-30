# kilix-agent-bench

An importable Python library and CLI for reproducible agent task evaluation in
disposable VMs. It freezes and provisions a selected Kilix stack, checks real
guest interfaces, and runs bounded controller sessions with independent outcome
scoring and native token accounting. Hosted and self-hosted controllers operate
as separate tracks feeding one dataset. Planning is the default; execution
requires an explicit flag and matching qualification evidence.

The base qualification copies a synthetic file containing shell punctuation and
Unicode, preserving every byte and adding no newline. An unprivileged guest
actor performs the copy. A separate privileged observer reads the resulting
files; the host checks them against its own expectations. A second control
deliberately adds a newline and must be scored as a task failure. Both controls
must match their expected outcomes for qualification to pass.

## Requirements and quick start

Use Linux on x86-64, Python 3.11 or newer, accessible `/dev/kvm`, QEMU,
`qemu-img`, OpenSSH client tools and `xorriso`. The initial backend requires KVM;
it does not silently switch to software emulation. Python runtime dependencies
are limited to the standard library. Controller isolation also requires
Bubblewrap. Action auditing requires an explicitly selected `strace` binary
compatible with the guest; its hash is included in the protocol lock.

From this checkout:

```sh
PYTHONPATH=src python3 -m kilix_agent_bench doctor --config examples/qualification.json
PYTHONPATH=src python3 -m kilix_agent_bench qualify --config examples/qualification.json
```

Or install the package in a virtual environment to obtain the CLI:

```sh
python3 -m venv .venv
.venv/bin/pip install .
.venv/bin/kilix-agent-bench doctor --config examples/qualification.json
.venv/bin/kilix-agent-bench qualify --config examples/qualification.json
```

The example pins a Debian cloud image by versioned URL and SHA-512. Its guest
uses two vCPUs, 1 GiB RAM and an 8 GiB writable disk. Image acquisition happens
on the host; guest outbound networking is disabled. The cloud-init seed creates
the actor and observer directories and provisions a unique SSH identity. No
operator home, display socket, SSH agent or model credentials enter the guest.

Qualification writes progress to stderr and one JSON result to stdout. Exit 0
means both scoring controls, disk reset and cleanup passed; exit 2 indicates an
error or failed qualification. Inspect the returned `report` path for all
observations, configuration, image and executable hashes, timestamps and
cleanup status. Failures remain in the run history.

## Storage and configuration

All runtime data defaults to `~/.local/gpu_terminal/kilix-agent-bench/`:

```text
images/    verified, read-only base images
stacks/    frozen software bundles and their configuration locks
vms/       owned disks, control sockets and private provisioning material
studies/   experiment definitions and reserved task partitions
mining/    explicitly imported session data and derived episodes
runs/      immutable-by-convention run histories and observations
reports/   generated comparisons
cache/     replaceable acquisition and build inputs
```

Use `--data-dir PATH` before the subcommand to override the root. Existing roots
must be owned by the invoking user and have mode 0700. VM IDs are opaque; names,
paths and endpoints are generated without referring to a named execution host.

Configuration uses `kilix-agent-bench/config-v1`; unknown fields are rejected.
The `image` object requires `sha512` and exactly one of `url` (HTTPS) or `path`
(relative to the configuration file). Inputs must be standalone, unencrypted
qcow2 images with cloud-init, Python 3, OpenSSH server and the fixture's basic
system commands. The `vm` object requires integer `cpus`, `memory_mib`,
`disk_gib` and `boot_timeout_s`. The disk must accommodate the base image.
Image downloads are bounded to 4 GiB and 900 seconds. A checksum verifies the
selected bytes; choose the image source and expected checksum independently of
untrusted task input.

## VM lifecycle and library

```sh
kilix-agent-bench vm create --config examples/qualification.json
kilix-agent-bench vm start VM_ID
kilix-agent-bench vm status VM_ID
kilix-agent-bench vm stop VM_ID
kilix-agent-bench vm reset VM_ID
kilix-agent-bench vm destroy VM_ID
```

Replace `VM_ID` with the returned ID. `create` prepares a stopped guest; `start`
waits for cloud-init and fixture readiness. `reset` discards the writable disk,
creates a fresh overlay and boots again. `destroy` removes that guest after
confirming its process stopped. Cached base images and run reports remain.

Lifecycle operations use per-VM locks and a shared allocation lock. Process
ownership checks bind the executable, unique VM name/UUID, opened disk and
Linux process start identity. A mismatched identity is an error; the tool does
not signal the process or delete its files. After an interrupted command, use
the recorded VM ID to inspect and stop or destroy its guest. Reported lifecycle
state and observed process liveness remain separate.

The same operations are available from `core.Store`, `core.load_config`,
`vm.VM`, `vm.doctor` and `qualification.qualify`. `tasks.verify` accepts an
observer record and host-owned expected bytes, independently of the actor.
The APIs and versioned records are experimental in 0.1.0.

## Frozen stack provisioning

Select reviewed source checkouts, a matching built engine, a complete set of
Debian dependency packages for the pinned base image, and the exact model
assets needed by the selected workflow. Capture takes explicit local inputs;
each repository is exported from its selected commit, including its pinned
submodules. The snapshot has independent Git objects and clean configuration.
Tracked working-copy edits, untracked files and existing Git credentials are
excluded. Each submodule's selected commit must be available locally.

```sh
kilix-agent-bench stack capture --inputs stack-inputs.json
kilix-agent-bench stack qualify --lock /path/to/returned-lock.json
```

`capture` writes a SHA-256-addressed tar bundle and JSON lock under `stacks/`.
The bundle binds the VM configuration, source identities, setup commands and
every payload/package file's bytes and mode. Both the host and guest verify
the member inventory before installation. The limit is 16 GiB and 100,000
files. Archive paths must be relative and normalized; links, special files,
duplicates, missing members and changed hashes are rejected. Artifact-tree
file symlinks are materialized only when their targets remain within that
tree; directory symlinks and Git snapshot symlinks are unsupported.

The input document uses `kilix-agent-bench/stack-inputs-v1` and requires:

| Field | Content |
| --- | --- |
| `benchmark_version` | Release family, such as `0.2.2` |
| `config` | The complete base-image/VM configuration described above |
| `repositories` | Entries with `path`, exact 40-digit `commit`, public HTTPS `repository` identity and relative `destination` |
| `trees` | Explicit artifact directories with `path` and relative `destination` |
| `files` | Explicit individual files with `path` and relative `destination` |
| `packages` | Local `.deb` files with `path` and exact `sha256` |
| `setup` | Steps with a unique lowercase `id`, an `argv` string array and `timeout_s` from 1 to 1800 |

Lists may be empty. Input paths resolve relative to the input document.
Destinations are below the guest actor's `~/.local/gpu_terminal/` and must not
overlap. Packages are installed offline using `dpkg`; setup commands then run
as the unprivileged actor with a clean environment and private home. Stack
inputs are trusted software: Debian maintainer scripts run as guest root.
No authentication state is imported automatically. Existing applicable model
license receipts can be selected explicitly; capture never creates acceptance.
Model assets, receipts, dependency packages and built binaries remain private
runtime artifacts and are not distributed in this source repository.

For an already-created guest with exactly the lock's VM configuration:

```sh
kilix-agent-bench stack provision VM_ID --lock /path/to/stack-lock.json
```

A failed provision remains recorded in that generation. Reset the guest
before retrying. Lifecycle locks prevent reset or destruction during
provisioning. Reset removes the installed software along with the rest of the
writable disk; the same frozen bundle can provision the new generation.

`stack qualify` creates its own guest, provisions it, checks the interfaces and
runs the positive literal-file control. It then resets the disk, repeats the
same provisioning and interface checks, runs the negative control and destroys
the guest. Evidence includes the package inventory, installed source identities,
engine selection/hash, commands, observed pane/process identities, actual
literal input bytes and cleanup results.

The initial interface profile expects this layout below the guest stack root:

```text
sources/kilix/
sources/kilix-desktops/kilix-tui-utils/
sources/kilix-apps/kilix-claudex/
kilix/build/current/source-id
kilix/build/current/src/
kilix/data/desktop-apps/kilix-needle/
kilix/data/desktop-apps/assets/
license-receipts/
```

Setup must install `kilix`, `kilix-needle`, `claudex` and the toolkit commands
in the actor's `~/.local/bin/`, and prepare the native support libraries.
The package closure must provide the engine's runtime libraries, development
tools required by those support libraries, and Xvfb. The profile checks engine
extension loading, grammar execution, rollout command reuse, model asset/license
readiness, literal pane input with separate submission, a single routed worker
and a two-session team. Coding clients in these checks are labeled process
fixtures. They record the actual delivered argv and cwd; they do not call a
provider or establish real agent readiness. Model asset readiness does not run
inference. Other workflow routes need their own qualification before scoring.

## Benchmark versions and later models

`benchmark_version` identifies the workflow release family, separately from
the Python package version and JSON schema versions. The current family is
`0.2.2` through that release. After release, changed workflow options can define
a `0.2.3` benchmark. Within a family, retain immutable protocol revisions and
hashes of tasks, route eligibility, guides, scorers and the stack lock.

Model, effort and client selections belong to the execution matrix, separate
from the fixed protocol. Adding a later model reuses the same tasks, seeds,
guides and software inputs. Record execution dates and provider-reported model
identities too: a stable model name alone does not freeze a remote service.

## Task routes and independent scoring

The development suite has six task types and sixteen task/route cells:

| Task | Routes | Observed result |
| --- | --- | --- |
| Literal file search | Native shell; Needle Files grammar CLI | Exact relative paths and first matching lines, excluding hidden files and symlinks |
| Literal pane input | Native remote control; `kilix pane send` | Exact UTF-8 bytes with no Enter or extra pane |
| Total RAM | Native shell; System CLI; Kilix grammar | Value and units checked against guest `/proc/meminfo` |
| Active log retrieval | Native process/open-file inspection; `kilix rollout log` | Correct active log and last assistant text, with stale decoys |
| One worker launch | Session-grid CLI; structured action CLI; Claudex; Ask grammar; Ask LFM2 model-only | Exactly one live fixture process and pane with the requested model, cwd and effort |
| Visible pane reading | Native remote control; `kilix watch` | Visible marker, excluding scrollback and without input |

Worker clients are labeled process fixtures: they verify delivery and launch
effects without creating child provider sessions. A pending launch receipt does
not override the independently observed process state. Outcome correctness and
required-route compliance are recorded separately; both are needed for an
eligible success. Every task also checks protected fixture hashes and unrelated
state. Positive and negative controls exercise the scorers.

```sh
kilix-agent-bench qualify-routes --lock /path/to/stack-lock.json --slots 2
```

This command defaults to fifteen routes without inference. Adding
`--allow-helper-inference` includes the local LFM2 model-only route. Qualification
records never enter the scored dataset. Each qualification slot owns one VM,
recreates its fixtures between controls, and destroys the guest on completion.

## Freeze, plan and execute

```sh
kilix-agent-bench study freeze --lock /path/to/stack-lock.json
kilix-agent-bench study manifest --protocol /path/to/protocol.json \
  --matrix /path/to/matrix.json --track hosted_api --rounds 1
kilix-agent-bench study manifest --protocol /path/to/protocol.json \
  --matrix /path/to/matrix.json --track vast_self_hosted --rounds 1
kilix-agent-bench run --manifest /path/to/manifest.json --slots 2
```

The final command only reports a plan. A protocol binds task generators,
scorers, common instructions, route cards, tool schema, budgets, stack bytes,
audit binary, harness source hashes and five paired development seeds. An
optional `--reserved` JSON file records a separate future evaluation partition;
it does not create additional scored cases. Keep private study plans and model
metadata under `studies/` in the runtime root.

A matrix contains `configurations`: each has a unique `id`, exact `model`,
`location` (`provider` or `self_hosted`), and a native client plus `effort` or a
self-hosted `profile`. Supported native clients are `codex`, `grok` and `claude`.
Self-hosted configurations also reference entries in `self_hosted_models`, each
with a `repository_revision` and `profiles` containing `id`, `settings`,
`precision`, `context_tokens` and optional hashed `metadata_artifacts`.
Artifact paths resolve relative to the matrix file. Settings, client executable
hashes and the complete schedule are copied into immutable manifests.

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

## Project rental spending

Project receipts live at
`~/.local/gpu_terminal/kilix-agent-bench/rentals/spending.jsonl` by default.
These commands only import and report records:

```sh
kilix-agent-bench rentals import-quote /path/to/best-of-3-receipt.json
kilix-agent-bench rentals record /path/to/owned-instance-or-billing-receipt.json
kilix-agent-bench rentals report
```

A receipt needs a stable `event_id`, timezone-qualified `at`, `kind`, source
identity and `source_sha256`. Register project ownership using `instance_created`
with `instance_id` and separate `compute_per_hour` and `storage_per_hour` rates.
Record `instance_state` transitions as `running`, `stopped` or `destroyed`.
Provider `charge` and `credit` receipts require nonnegative `usd` amounts and a
unique `billing_id`. Only registered project instances can receive those rows.

Quotes, time/rate estimates and imported charges remain separate. Storage can
continue while compute is stopped. Unknown billing intervals are reported,
unrecorded transfers are excluded, and estimates are never added to charges.
The report covers imported project receipts, not the account's entire balance.

## Qualification boundaries

Base-only `qualify` records `kilix_stack_qualified: false`. Successful
`stack qualify` records the tested interface scope explicitly. It covers the
guest OS interfaces and model asset readiness described above. The route
controls and offline adapter tests extend that coverage; they do not establish
live provider readiness, guide savings or a working rented inference image.
Those require separate qualification before a scored study.
Select and freeze compatible source and executable versions during experiment
preparation. A capture is a replayable software artifact, not a claim that its
binary can be rebuilt bit for bit from source.

VM memory and disk admission are local checks, not a scheduler for unrelated
host workloads. KVM, the host account and provisioning inputs are trusted.
Guest isolation and observer separation here are intended for controlled task
fixtures; they are not a qualification for adversarial hypervisor workloads.

Run the contract tests without starting a guest or making model calls:

```sh
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

The native image tests use `qemu-img` if available. `qualify` is the separate
integration check that creates a real guest and verifies both controls and
cleanup.

## Upstream interfaces

The VM backend uses [QEMU's documented invocation options](https://www.qemu.org/docs/master/system/invocation.html)
and [cloud-init's NoCloud seed format](https://docs.cloud-init.io/en/latest/reference/datasources/nocloud.html).
The example image and its checksums are published in the
[Debian cloud image directory](https://cloud.debian.org/images/cloud/trixie/20261026-2643/).

The library source is licensed under [MIT](LICENSE). Guest images and external
executables retain their own licenses and are not redistributed in this
repository.
