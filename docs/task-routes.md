# Task routes and independent scoring

Reference for [kilix-agent-bench](../README.md).

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
