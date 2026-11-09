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

**Status:** a standalone evaluation harness, version 0.1.0. Plebian-OS, Kilix
and the Kilix content catalog do not install it or reference it. It measures
Kilix stacks from the outside: it freezes chosen Kilix source commits and
packages, installs them into a disposable guest and scores agent tasks there.

## What it does

- Creates, resets and destroys owned QEMU/KVM guests from a checksum-pinned
  Debian cloud image, with guest networking off.
- Freezes a selected Kilix stack into a SHA-256-addressed bundle and provisions
  it offline into a guest (`stack capture`, `stack provision`).
- Qualifies the base VM, the stack's interfaces and each task route with
  positive and negative controls before anything is scored.
- Plans, and with explicit readiness evidence executes, controller sessions
  across a task/route/model matrix in two tracks (hosted and self-hosted).
- Scores outcomes from an independent observer and reports controller tokens
  per verified success, plus timings and project rental spending.

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

## Usage

| Command | What it does |
|---|---|
| `doctor --config FILE`, `qualify --config FILE` | Check the host; run the base VM qualification |
| `vm create\|start\|status\|stop\|reset\|destroy` | Guest lifecycle |
| `stack capture --inputs FILE`, `stack qualify --lock FILE`, `stack provision VM_ID --lock FILE` | Freeze, qualify and install a Kilix stack |
| `qualify-routes --lock FILE --slots N` | Route controls without scoring |
| `study freeze`, `study manifest` | Bind a protocol and expand a matrix into a manifest |
| `run --manifest FILE [--execute --readiness FILE]` | Report a plan, or execute it |
| `recover-attempt DIR`, `dataset report\|ingest`, `timings FILE` | Recovery and reporting |
| `rentals import-quote\|record\|report` | Project rental spending records |

`--data-dir PATH` before the subcommand moves the runtime root.

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

Execution receipts, self-hosted endpoints, recovery and the evidence each
attempt retains are described in
[docs/execution-and-evidence.md](docs/execution-and-evidence.md).

## How it works

```
 host                                         disposable guest (KVM)
 ----                                         ----------------------
 core.Store  private runtime root             cloud-init seed: actor + observer
 vm.VM       qemu, overlay disk, SSH  ----->  unprivileged actor runs the task
 stack       frozen tar bundle + lock ----->  offline dpkg + setup commands
 runner      bounded trials, two slots        privileged observer reads results
 tasks       host-owned expected bytes <----  observation record
 dataset     append-only attempts; usage.py normalizes token counts
```

Every scored attempt gets a freshly provisioned guest. The controller acts
only through the guest command tool; a separate privileged observer records
before/after state, and the host compares that record with expectations it
computed itself, so the actor cannot grade its own work. Qualification
records never enter the scored dataset. Library entry points are
`core.Store`, `core.load_config`, `vm.VM`, `vm.doctor`,
`qualification.qualify` and `tasks.verify`.

## Documentation

- [docs/storage-and-vms.md](docs/storage-and-vms.md) — runtime root layout, configuration schema, VM lifecycle and locks
- [docs/stack-provisioning.md](docs/stack-provisioning.md) — stack inputs, bundle verification, interface profile, benchmark versions
- [docs/task-routes.md](docs/task-routes.md) — the six task types, their routes and independent scoring
- [docs/execution-and-evidence.md](docs/execution-and-evidence.md) — execution readiness, self-hosted endpoints, recovery, evidence and measurement
- [docs/rental-spending.md](docs/rental-spending.md) — importing and reporting project rental receipts
- [docs/qualification-boundaries.md](docs/qualification-boundaries.md) — what each qualification does and does not establish, contract tests

## Related repositories

- [kilix](https://github.com/itsmygithubacct/kilix#readme) — the terminal and verbs a frozen stack installs
- [kilix-needle](https://github.com/itsmygithubacct/kilix-needle#readme) — Needle grammar and Ask routes exercised by the task routes
- [kilix-claudex](https://github.com/itsmygithubacct/kilix-claudex#readme) — session routes and teams checked by the interface profile
- [kilix-tui-utils](https://github.com/itsmygithubacct/kilix-tui-utils#readme) — toolkit commands the stack layout installs

## Part of Plebian-OS

kilix-agent-bench is one of the repositories published alongside
[Plebian-OS](https://github.com/itsmygithubacct/plebian-os#readme), a Debian
install whose desktop is [Kilix](https://github.com/itsmygithubacct/kilix#readme)
running the [Kilix 95](https://github.com/itsmygithubacct/kilix-95#readme)
desktop through [Pleb](https://github.com/itsmygithubacct/pleb#readme).
It is not installed on Plebian-OS; it is the harness used to evaluate Kilix
stacks in disposable VMs. The map of every repository and how it ships is on
the [components page](https://github.com/itsmygithubacct/plebian-os/blob/main/docs/components.md).

## Upstream interfaces

The VM backend uses [QEMU's documented invocation options](https://www.qemu.org/docs/master/system/invocation.html)
and [cloud-init's NoCloud seed format](https://docs.cloud-init.io/en/latest/reference/datasources/nocloud.html).
The example image and its checksums are published in the
[Debian cloud image directory](https://cloud.debian.org/images/cloud/trixie/20261026-2643/).

## License

The library source is licensed under [MIT](LICENSE). Guest images and external
executables retain their own licenses and are not redistributed in this
repository.
