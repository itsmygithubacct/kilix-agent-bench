# kilix-agent-bench

An importable Python library and CLI for reproducible agent task evaluation in
disposable VMs. The first implementation qualifies VM creation, provisioning,
reset, observation and cleanup using deterministic fixture actors. It makes no
model calls and does not yet run a scored agent benchmark.

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

## Qualification boundaries

This version qualifies a base OS fixture. It records
`kilix_stack_qualified: false`: it does not install or qualify a complete Kilix
stack, provider clients, model assets, agent routes, guide changes or token
accounting. Those require separate adapters and qualification before a scored
model study. Rebuilding a current compatible stack and freezing its binaries
belongs to experiment preparation, not to updating a running matrix.

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
