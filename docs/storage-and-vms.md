# Storage, configuration and VM lifecycle

Reference for [kilix-agent-bench](../README.md).

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
