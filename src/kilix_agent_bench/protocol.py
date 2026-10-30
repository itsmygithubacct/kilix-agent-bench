"""Immutable protocol locks and independently resumable execution manifests."""
import json
from pathlib import Path
import random

from .bridge import EXECUTE_TOOL
from .clients import SYSTEM, executable
from .core import BenchError, atomic_json, digest, private_dir
from .scenarios import GUIDE, ROUTE_CARDS, TASK_ROUTES
from .stack import load_lock
from .telemetry import content_hash


DEFAULT_LIMITS = {"max_tool_calls": 12, "tool_timeout_s": 90, "tool_output_bytes": 16384,
                  "max_model_turns": 12, "trial_timeout_s": 600, "max_output_tokens": 4096,
                  "request_timeout_s": 120, "max_controller_tokens": 100000}


def harness_hashes():
    return {p.name: digest(p) for p in sorted(Path(__file__).parent.glob("*.py"))}


def freeze(store, lock_path, *, trace_binary, reserved=None):
    lock_path, trace_binary = Path(lock_path).resolve(), Path(trace_binary).resolve()
    lock, _, _, config = load_lock(lock_path)
    if not lock_path.is_relative_to(store.root / "stacks"):
        raise BenchError("Capture the selected stack in this runtime store before freezing")
    value = {"schema": "kilix-agent-bench/protocol-v1", "benchmark_version": "0.2.2", "revision": 1,
             "stack_lock": lock_path.name, "stack_lock_sha256": digest(lock_path), "vm_config": config,
             "harness_sha256": harness_hashes(), "trace_binary": str(trace_binary), "trace_binary_sha256": digest(trace_binary),
             "tasks": TASK_ROUTES, "guide": GUIDE, "route_cards": ROUTE_CARDS,
             "system": SYSTEM, "execute_tool": EXECUTE_TOOL, "limits": DEFAULT_LIMITS,
             "development_seeds": [220200, 220201, 220202, 220203, 220204],
             "reserved_partition": reserved or [], "rounds": 5,
             "pairing": "Same task/seed across routes for each configuration; fresh session and VM disk generation for each attempt.",
             "order": "Deterministically shuffled within repetition, recorded in each track manifest.",
             "retry_policy": "No automatic retry of completed task failures. Interrupted/infrastructure-invalid attempts remain recorded; resume creates a new attempt under the same logical trial ID.",
             "token_limit_policy": "Stop between observable responses at the total-token or model-turn bound. An in-flight response may overshoot. Native client output limits may be unavailable; record that capability rather than inventing enforcement.",
             "first_round_policy": "One development observation per task/route/configuration. If protocol or harness changes after inspection, retain records under the original revision and regenerate affected manifests."}
    sha = content_hash(value)
    directory = private_dir(store.root / "studies" / "protocols")
    path = directory / (sha + ".json")
    if path.exists() and json.loads(path.read_text()) != value:
        raise BenchError("Protocol lock identity conflict")
    atomic_json(path, value)
    return {"protocol": str(path), "sha256": sha, "task_route_cells": sum(map(len, TASK_ROUTES.values()))}


def load_protocol(store, path, *, verify_harness=True):
    value = json.loads(Path(path).read_text())
    if value.get("schema") != "kilix-agent-bench/protocol-v1":
        raise BenchError("Unsupported scored protocol")
    if Path(value["stack_lock"]).name != value["stack_lock"]:
        raise BenchError("Stack lock must be a runtime-store filename")
    stack = store.root / "stacks" / value["stack_lock"]
    if digest(stack) != value["stack_lock_sha256"]:
        raise BenchError("Frozen stack lock changed")
    if digest(value["trace_binary"]) != value["trace_binary_sha256"]:
        raise BenchError("Frozen execution-audit binary changed")
    if verify_harness and harness_hashes() != value["harness_sha256"]:
        raise BenchError("Harness changed after protocol freeze; create a new protocol revision")
    if value["tasks"] != TASK_ROUTES or value["guide"] != GUIDE or value["route_cards"] != ROUTE_CARDS:
        raise BenchError("Runtime task or guide definitions differ from the frozen protocol")
    return value, stack


def build_manifest(store, protocol_path, matrix_path, track, *, rounds=1):
    if track not in {"hosted_api", "vast_self_hosted"}:
        raise BenchError("Select hosted_api or vast_self_hosted")
    protocol, _ = load_protocol(store, protocol_path)
    if type(rounds) is not int or not 1 <= rounds <= protocol["rounds"]:
        raise BenchError("Invalid repetition count")
    matrix = json.loads(Path(matrix_path).read_text())
    location = "provider" if track == "hosted_api" else "self_hosted"
    configurations = [dict(c) for c in matrix["configurations"] if c["location"] == location]
    if not configurations or len({c["id"] for c in configurations}) != len(configurations):
        raise BenchError("Empty or duplicate controller matrix")
    local = {c["model"]: c for c in matrix.get("self_hosted_models", [])}
    for configuration in configurations:
        if track == "hosted_api":
            binary = executable(configuration["client"])
            configuration["client_binary_sha256"] = digest(binary)
        else:
            profile = next((p for p in local[configuration["model"]]["profiles"] if p["id"] == configuration["profile"]), None)
            if not profile or not isinstance(profile.get("settings"), dict):
                raise BenchError("Freeze native settings for every self-hosted configuration first")
            configuration.update(settings=profile["settings"], precision=profile["precision"],
                                 context_tokens=profile["context_tokens"],
                                 metadata_artifacts=profile.get("metadata_artifacts", {}),
                                 repository_revision=local[configuration["model"]]["repository_revision"])
            for artifact in configuration["metadata_artifacts"].values():
                if artifact.get("sha256") and digest(Path(matrix_path).parent / artifact["path"]) != artifact["sha256"]:
                    raise BenchError("Pinned local-model metadata changed")
    protocol_sha = content_hash(protocol)
    cells = []
    for repetition in range(rounds):
        current = []
        for configuration in configurations:
            for task, routes in protocol["tasks"].items():
                for route in routes:
                    cell = {"configuration_id": configuration["id"], "configuration_sha256": content_hash(configuration), "task": task, "route": route,
                            "repetition": repetition, "seed": protocol["development_seeds"][repetition]}
                    cell["logical_trial_id"] = content_hash({"protocol": protocol_sha, "track": track, **cell})[:24]
                    current.append(cell)
        random.Random(f"{protocol_sha}/{track}/{repetition}").shuffle(current)
        cells.extend(current)
    value = {"schema": "kilix-agent-bench/execution-manifest-v1", "benchmark_version": "0.2.2",
             "protocol_path": str(Path(protocol_path).resolve()), "protocol_sha256": protocol_sha,
             "matrix_sha256": content_hash(configurations), "track": track, "configurations": configurations,
             "repetitions": rounds, "cells": cells, "scheduled_trials": len(cells),
             "scored_execution_authorized": False, "live_qualification_required": True,
             "self_hosted_scheduling": "Filter a manifest by configuration while its pinned model is loaded; all selected cells retain their fixed logical IDs." if track == "vast_self_hosted" else None}
    ident = content_hash(value)
    root = private_dir(store.root / "studies" / "manifests")
    path = root / (ident + ".json")
    atomic_json(path, value)
    return {"manifest": str(path), "sha256": ident, "track": track, "scheduled_trials": len(cells)}


def validate_manifest(manifest, protocol):
    """Reject edited cells, changed settings and incomplete schedules before allocation."""
    try:
        if manifest["schema"] != "kilix-agent-bench/execution-manifest-v1" or manifest["track"] not in {"hosted_api", "vast_self_hosted"}:
            raise ValueError("schema or track")
        configurations = manifest["configurations"]
        rounds = manifest["repetitions"]
        if type(rounds) is not int or not 1 <= rounds <= protocol["rounds"]:
            raise ValueError("repetitions")
        if not configurations or len({c["id"] for c in configurations}) != len(configurations):
            raise ValueError("configuration identities")
        if content_hash(configurations) != manifest["matrix_sha256"] or content_hash(protocol) != manifest["protocol_sha256"]:
            raise ValueError("input hash")
        expected = {}
        for configuration in configurations:
            for repetition in range(rounds):
                for task, routes in protocol["tasks"].items():
                    for route in routes:
                        cell = {"configuration_id": configuration["id"], "configuration_sha256": content_hash(configuration),
                                "task": task, "route": route, "repetition": repetition,
                                "seed": protocol["development_seeds"][repetition]}
                        ident = content_hash({"protocol": manifest["protocol_sha256"], "track": manifest["track"], **cell})[:24]
                        expected[ident] = {**cell, "logical_trial_id": ident}
        cells = manifest["cells"]
        if len(cells) != len(expected) or manifest["scheduled_trials"] != len(cells):
            raise ValueError("schedule size")
        if len({c["logical_trial_id"] for c in cells}) != len(cells):
            raise ValueError("duplicate logical trial")
        if any(expected.get(c["logical_trial_id"]) != c for c in cells):
            raise ValueError("cell identity")
    except (KeyError, TypeError, ValueError) as exc:
        raise BenchError("Execution manifest does not match the frozen protocol and matrix") from exc
