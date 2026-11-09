# Project rental spending

Reference for [kilix-agent-bench](../README.md).

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
