"""Project rental receipts. Estimates and recorded charges stay separate."""
from datetime import datetime
from decimal import Decimal, InvalidOperation
import fcntl
import json
import os
from pathlib import Path

from .core import BenchError, digest, private_dir
from .telemetry import append_event, content_hash, read_events, timestamp


KINDS = {"quote", "instance_created", "instance_state", "charge", "credit"}


def money(value):
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise BenchError("Invalid monetary amount") from exc
    if not result.is_finite() or result < 0:
        raise BenchError("Monetary amounts must be finite and nonnegative")
    return result


def instant(value):
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None:
            raise ValueError("Timezone required")
        return result
    except (ValueError, TypeError, AttributeError) as exc:
        raise BenchError("A timestamp with an explicit timezone is required") from exc


class RentalLedger:
    def __init__(self, store):
        self.root = private_dir(store.root / "rentals")
        self.path = self.root / "spending.jsonl"

    def record(self, receipt):
        fd = os.open(self.root / "spending.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            return self._record(receipt)
        finally:
            os.close(fd)

    def _record(self, receipt):
        allowed = {"event_id", "kind", "at", "instance_id", "state", "rates", "usd", "source",
                   "source_sha256", "billing_id", "period_start", "period_end", "quote", "note"}
        if not isinstance(receipt, dict) or set(receipt) - allowed:
            raise BenchError("Unknown rental receipt fields")
        kind = receipt.get("kind")
        if kind not in KINDS or not isinstance(receipt.get("event_id"), str) or not receipt["event_id"]:
            raise BenchError("Receipt requires a stable event_id and a supported kind")
        instant(receipt.get("at"))
        if not receipt.get("source") or not receipt.get("source_sha256"):
            raise BenchError("Receipt requires its source identity and SHA-256")
        if len(receipt["source_sha256"]) != 64 or any(c not in "0123456789abcdef" for c in receipt["source_sha256"]):
            raise BenchError("Invalid receipt source hash")
        existing = read_events(self.path)
        owned = {r["instance_id"] for r in existing if r["kind"] == "instance_created"}
        if kind == "instance_created":
            if not str(receipt.get("instance_id", "")).isdigit():
                raise BenchError("Instance receipt requires its numeric instance ID")
            rates = receipt.get("rates", {})
            if set(rates) != {"compute_per_hour", "storage_per_hour"}:
                raise BenchError("Record compute and storage hourly rates separately")
            for rate in rates.values():
                money(rate)
            if receipt["instance_id"] in owned and not any(r == receipt for r in existing):
                raise BenchError("Instance ownership/rates already registered")
        elif kind != "quote":
            if receipt.get("instance_id") not in owned:
                raise BenchError("Register project ownership before adding instance receipts")
        if kind == "instance_state" and receipt.get("state") not in {"running", "stopped", "destroyed"}:
            raise BenchError("Unsupported instance billing state")
        if kind == "instance_state":
            previous = [r for r in existing if r.get("instance_id") == receipt["instance_id"] and r["kind"] in {"instance_created", "instance_state"}]
            if any(instant(r["at"]) > instant(receipt["at"]) for r in previous):
                raise BenchError("State receipt is older than a recorded state; reconcile explicitly")
            if any(r.get("state") == "destroyed" for r in previous) and receipt not in existing:
                raise BenchError("Destroyed instances cannot resume billing states")
        if kind in {"charge", "credit"}:
            money(receipt.get("usd"))
            if not receipt.get("billing_id"):
                raise BenchError("A stable provider billing-line ID is required")
            for row in existing:
                if row.get("billing_id") == receipt["billing_id"] and row["event_id"] != receipt["event_id"]:
                    raise BenchError("Provider billing line was already imported")
        append_event(self.path, receipt, unique_key="event_id")
        return self.report()

    def import_quote(self, path):
        path = Path(path)
        value = json.loads(path.read_text())
        source_hash = digest(path)
        return self.record({"event_id": "quote-" + source_hash, "kind": "quote",
                            "at": value.get("checked_at", value.get("created_at", timestamp())),
                            "source": str(path), "source_sha256": source_hash,
                            "quote": value, "note": "Selection estimate only; creates no rental or charge."})

    def report(self, *, at=None):
        at = at or timestamp()
        until = instant(at)
        rows = read_events(self.path)
        owned = {r["instance_id"]: r for r in rows if r["kind"] == "instance_created"}
        charges = sum((money(r["usd"]) for r in rows if r["kind"] == "charge"), Decimal(0))
        credits = sum((money(r["usd"]) for r in rows if r["kind"] == "credit"), Decimal(0))
        instances, estimate = [], Decimal(0)
        for ident, create in sorted(owned.items()):
            events = sorted([r for r in rows if r.get("instance_id") == ident and r["kind"] == "instance_state"],
                            key=lambda r: instant(r["at"]))
            # Creation itself does not prove when running billing began.
            state, cursor, running_s, storage_s = "unknown", instant(create["at"]), 0.0, 0.0
            unknown = False
            for event in [*events, {"at": at, "state": state}]:
                end = min(instant(event["at"]), until)
                seconds = max(0, (end - cursor).total_seconds())
                if state == "running":
                    running_s += seconds
                    storage_s += seconds
                elif state == "stopped":
                    storage_s += seconds
                elif state == "unknown" and seconds:
                    unknown = True
                if end >= until:
                    break
                state, cursor = event["state"], end
            rates = create["rates"]
            subtotal = (Decimal(str(running_s)) * money(rates["compute_per_hour"]) +
                        Decimal(str(storage_s)) * money(rates["storage_per_hour"])) / Decimal(3600)
            estimate += subtotal
            instances.append({"instance_id": ident, "state": state, "running_s": running_s,
                              "storage_s": storage_s, "estimated_compute_storage_usd": str(subtotal),
                              "unknown_billing_intervals": unknown})
        return {"schema": "kilix-agent-bench/rental-spending-v1", "as_of": at,
                "ledger": str(self.path), "ledger_content_sha256": content_hash(rows),
                "quote_count": sum(r["kind"] == "quote" for r in rows), "instances": instances,
                "recorded_charges_usd": str(charges), "recorded_credits_usd": str(credits),
                "recorded_net_usd": str(charges - credits),
                "estimated_compute_storage_usd": str(estimate),
                "billing_coverage": "no_instances_registered" if not owned else "only_imported_provider_lines",
                "note": "Estimates exclude unrecorded transfers and unknown intervals. Do not add estimates to charges."}
