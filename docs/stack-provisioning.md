# Frozen stack provisioning

Reference for [kilix-agent-bench](../README.md).

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
