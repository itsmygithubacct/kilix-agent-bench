# kilix-agent-bench

An importable Python library and CLI for reproducible agent task evaluation in
disposable VMs. It qualifies VM creation, provisioning, reset, observation and
cleanup using deterministic fixture actors. It can also freeze a selected Kilix
stack and provision it offline, then check real guest pane and session-grid
interfaces. Qualification makes no model calls. Scored model execution and
usage accounting remain future work.

The qualification copies a synthetic file containing shell punctuation and
Unicode, preserving every byte and adding no newline. An unprivileged guest
actor performs the copy. A separate privileged observer reads the resulting
files; the host checks them against its own expectations. A second control
deliberately adds a newline and must be scored as a task failure. Both controls
must match their expected outcomes for qualification to pass.

## Requirements and quick start

Use Linux on x86-64, Python 3.11 or newer, accessible `/dev/kvm`, QEMU,
`qemu-img`, OpenSSH client tools and `xorriso`. The initial backend requires KVM;
it does not silently switch to software emulation. Python runtime dependencies
are limited to the standard library.

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
No scored runner or provider adapter is implemented by these configuration
conventions.

## Qualification boundaries

Base-only `qualify` records `kilix_stack_qualified: false`. Successful
`stack qualify` records the tested interface scope explicitly. It covers the
guest OS interfaces and model asset readiness described above; real provider
clients, model inference, remaining action routes, guide comparisons and token
accounting require separate adapters and qualification before a scored study.
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
