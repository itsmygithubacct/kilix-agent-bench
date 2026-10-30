"""One append-only dataset for independently operated controller tracks."""
from collections import defaultdict
import json
from pathlib import Path
import re

from .core import BenchError, atomic_json, digest, private_dir
from .telemetry import append_event, content_hash, read_events, summarize_timings


def finalize(path):
    path = Path(path)
    inventory = {}
    for item in sorted(path.rglob("*")):
        if item.is_symlink():
            raise BenchError("Finalized evidence cannot contain symlinks")
        if item.is_file() and item != path / "checksums.json":
            inventory[str(item.relative_to(path))] = {"sha256": digest(item), "bytes": item.stat().st_size}
    atomic_json(path / "checksums.json", {"schema": "kilix-agent-bench/evidence-v1", "files": inventory})
    return digest(path / "checksums.json")


def verify_evidence(path):
    path = Path(path)
    manifest = json.loads((path / "checksums.json").read_text())
    if manifest.get("schema") != "kilix-agent-bench/evidence-v1":
        raise BenchError("Unsupported evidence manifest")
    items = list(path.rglob("*"))
    if any(p.is_symlink() for p in items):
        raise BenchError("Finalized evidence cannot contain symlinks")
    actual_files = {str(p.relative_to(path)) for p in items if p.is_file() and p != path / "checksums.json"}
    if actual_files != set(manifest["files"]):
        raise BenchError("Finalized evidence inventory changed")
    for name, record in manifest["files"].items():
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts:
            raise BenchError("Invalid evidence member path")
        target = path / relative
        if target.is_symlink() or not target.resolve().is_relative_to(path.resolve()):
            raise BenchError("Evidence member left the attempt directory")
        if target.stat().st_size != record["bytes"] or digest(target) != record["sha256"]:
            raise BenchError("Finalized evidence bytes changed: " + name)
    return digest(path / "checksums.json")


class Dataset:
    def __init__(self, store, name="0.2.2-route-discovery"):
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,100}", name):
            raise BenchError("Invalid dataset ID")
        private_dir(store.root / "datasets")
        self.root = private_dir(store.root / "datasets" / name)
        self.rows = self.root / "attempts.jsonl"

    def ingest(self, path):
        path = Path(path)
        evidence_hash = verify_evidence(path)
        result = json.loads((path / "result.json").read_text())
        if result.get("schema") != "kilix-agent-bench/attempt-v1" or result.get("run_kind") != "scored":
            raise BenchError("Only explicitly scored attempts enter the benchmark dataset")
        required = {"attempt_id", "logical_trial_id", "track", "configuration_id", "protocol_sha256", "manifest_sha256",
                    "task", "route", "seed", "repetition", "outcome", "usage", "cleanup"}
        if required - set(result):
            raise BenchError("Incomplete attempt provenance")
        row = {**result, "source_directory": str(path.resolve()), "evidence_sha256": evidence_hash}
        added = append_event(self.rows, row, unique_key="attempt_id")
        return {"added": added, "attempt_id": result["attempt_id"], "dataset": str(self.root)}

    def report(self):
        rows = read_events(self.rows)
        groups = defaultdict(list)
        for row in rows:
            groups[(row["protocol_sha256"], row["track"], row["configuration_id"], row["task"], row["route"])].append(row)
        summaries = []
        for key, attempts in sorted(groups.items()):
            valid = [r for r in attempts if r["outcome"].get("outcome") in {"valid_pass", "valid_fail"}]
            successes = [r for r in valid if r["outcome"].get("eligible_success") and r["cleanup"] == "passed"]
            complete = bool(valid) and all(r["usage"].get("ranking_total_tokens") is not None for r in valid)
            tokens = sum(r["usage"]["ranking_total_tokens"] for r in valid) if complete else None
            summaries.append({"protocol_sha256": key[0], "track": key[1], "configuration_id": key[2],
                              "task": key[3], "route": key[4], "attempts": len(attempts), "valid_attempts": len(valid),
                              "invalid_attempts": len(attempts) - len(valid), "verified_successes": len(successes),
                              "usage_complete": complete, "controller_tokens_including_failures": tokens,
                              "controller_tokens_per_verified_success": tokens / len(successes) if tokens is not None and successes else None})
        result = {"schema": "kilix-agent-bench/dataset-report-v1", "attempts": len(rows),
                  "dataset_sha256": content_hash(rows), "groups": summaries,
                  "interpretation": "Compare routes within each controller configuration and protocol. Missing usage and zero successes do not produce a token-saving claim."}
        atomic_json(self.root / "summary.json", result)
        return result
