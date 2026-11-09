# Qualification boundaries

Reference for [kilix-agent-bench](../README.md).

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
