"""Bounded VM trials, independent tracks, durable progress and explicit execution."""
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import threading
import time
import uuid

from .clients import executable, native_client, self_hosted_client, validate_endpoint
from .core import BenchError, atomic_json, digest, private_dir
from .dataset import Dataset, finalize, verify_evidence
from .protocol import load_protocol, validate_manifest
from .scenarios import case, prompt
from .stack import provision
from .telemetry import Timings, append_event, content_hash, read_events, timestamp
from .trial import GuestTrial
from .usage import aggregate
from .vm import VM


@contextmanager
def slot_lease(store, cancelled):
    """Two slots across both tracks, also across separate runner processes."""
    owned = None
    try:
        while owned is None:
            if cancelled.is_set():
                raise InterruptedError("Batch cancelled while waiting for a VM slot")
            for slot in range(2):
                fd = os.open(store.root / "cache" / f"trial-slot-{slot}.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    os.close(fd)
                    continue
                owned = (slot, fd)
                break
            if owned is None:
                cancelled.wait(0.1)
        yield owned[0]
    finally:
        if owned is not None:
            os.close(owned[1])


def validate_readiness(manifest, receipt, *, slots):
    if receipt.get("schema") != "kilix-agent-bench/execution-readiness-v1" or receipt.get("protocol_sha256") != manifest["protocol_sha256"]:
        raise BenchError("A matching execution-readiness receipt is required before model trials")
    if slots > 1 and receipt.get("two_slots_verified") is not True:
        raise BenchError("Two-slot execution has not been qualified")
    qualified_routes = {tuple(x) for x in receipt.get("qualified_routes", [])}
    for cell in manifest["cells"]:
        if (cell["task"], cell["route"]) not in qualified_routes:
            raise BenchError("An eligible route still needs execution qualification")
    for configuration in manifest["configurations"]:
        proof = receipt.get("configurations", {}).get(configuration["id"], {})
        if any(proof.get(key) is not True for key in ("model_accepted", "settings_verified", "usage_complete", "vm_tool_verified")):
            raise BenchError("Controller configuration still needs live qualification: " + configuration["id"])
        if proof.get("configuration_sha256") != content_hash(configuration):
            raise BenchError("Controller qualification does not match the frozen settings")


def run_attempt(store, protocol, stack, manifest, cell, *, cancelled, endpoint=None, driver=None, run_kind="scored"):
    if driver is not None and run_kind != "qualification":
        raise BenchError("Injected deterministic drivers cannot produce scored data")
    ident = "attempt-" + uuid.uuid4().hex[:12]
    path = private_dir(store.root / "runs" / ident)
    configuration = next(c for c in manifest["configurations"] if c["id"] == cell["configuration_id"])
    timings = Timings(path / "timings.jsonl", attempt_id=ident, track=manifest["track"], configuration_id=configuration["id"])
    result = {"schema": "kilix-agent-bench/attempt-v1", "run_kind": run_kind, "attempt_id": ident,
              **cell, "track": manifest["track"], "protocol_sha256": manifest["protocol_sha256"],
              "manifest_sha256": content_hash(manifest), "configuration": configuration,
              "started_at": timestamp(), "status": "running", "cleanup": "pending",
              "usage": aggregate([]), "outcome": {"outcome": "invalid_infrastructure", "eligible_success": False}}
    atomic_json(path / "result.json", result)
    vm = None
    began = time.monotonic()
    try:
        with timings.span("complete_trial"):
            with timings.span("slot_queue"):
                lease = slot_lease(store, cancelled)
                slot = lease.__enter__()
            try:
                result["slot"] = slot
                with timings.span("vm_create_boot_provision"):
                    vm = VM.create(store, protocol["vm_config"])
                    result["vm_id"] = vm.ident
                    atomic_json(path / "result.json", result)
                    vm.start()
                    provision(vm, stack)
                if cancelled.is_set():
                    raise InterruptedError("Batch cancelled after VM preparation")
                spec = case(cell["task"], cell["seed"])
                trial = GuestTrial(vm, path, spec, cell["route"], timings, limits=protocol["limits"])
                with timings.span("observer_install"):
                    result["observer_payloads"] = trial.install(protocol["trace_binary"])
                before = trial.setup()
                task_prompt = prompt(spec, before, cell["route"])
                atomic_json(path / "task.json", {"scenario": spec, "prompt": task_prompt, "prompt_sha256": content_hash(task_prompt)})
                if driver is not None:
                    if run_kind != "qualification":
                        raise BenchError("Injected deterministic drivers cannot produce scored data")
                    controller = driver(configuration, task_prompt, trial.execute, path, timings, protocol["limits"])
                elif manifest["track"] == "hosted_api":
                    if digest(executable(configuration["client"])) != configuration["client_binary_sha256"]:
                        raise BenchError("Installed controller client changed after manifest freeze")
                    controller = native_client(configuration, task_prompt, trial.execute, path, timings,
                                               protocol["limits"], cancelled=cancelled)
                else:
                    if endpoint is None:
                        raise BenchError("A pinned self-hosted endpoint is required")
                    controller = self_hosted_client(configuration, task_prompt, trial.execute, path, timings,
                                                    protocol["limits"], endpoint, cancelled=cancelled)
                result["usage"] = controller["usage"]
                result["controller"] = {k: v for k, v in controller.items() if k != "usage"}
                result["outcome"] = trial.finish(controller["answer"])
                result["tool_calls"] = len(trial.calls)
                if controller.get("timed_out") or controller["exit_code"]:
                    result["outcome"].update(outcome="valid_fail", eligible_success=False)
                    result["outcome"]["checks"]["controller_completed"] = False
                result["status"] = "completed"
            finally:
                try:
                    if vm is not None:
                        with timings.span("vm_cleanup"):
                            vm.destroy()
                        result["cleanup"] = "passed"
                    else:
                        result["cleanup"] = "no_guest_started"
                finally:
                    lease.__exit__(None, None, None)
    except BaseException as exc:
        result.update(status="interrupted" if isinstance(exc, (KeyboardInterrupt, InterruptedError)) else "invalid_infrastructure",
                      error=f"{type(exc).__name__}: {exc}")
        if result["cleanup"] == "pending":
            result["cleanup"] = "failed_or_unknown"
        result["outcome"] = {"outcome": "invalid_infrastructure", "eligible_success": False}
    result.update(completed_at=timestamp(), elapsed_s=time.monotonic() - began)
    atomic_json(path / "result.json", result)
    finalize(path)
    return path, result


def run_manifest(store, manifest_path, *, execute=False, readiness=None, slots=1, configuration_id=None,
                 endpoint=None, progress=print):
    manifest = json.loads(Path(manifest_path).read_text())
    if manifest.get("schema") != "kilix-agent-bench/execution-manifest-v1" or slots not in (1, 2):
        raise BenchError("Invalid execution manifest or slot count")
    protocol, stack = load_protocol(store, manifest["protocol_path"])
    validate_manifest(manifest, protocol)
    configurations = {c["id"]: c for c in manifest["configurations"]}
    if configuration_id is not None and configuration_id not in configurations:
        raise BenchError("Requested configuration is not in the manifest")
    cells = [c for c in manifest["cells"] if configuration_id is None or c["configuration_id"] == configuration_id]
    if not execute:
        return {"status": "plan_only", "track": manifest["track"], "trials": len(cells), "slots": slots,
                "model_calls": 0, "note": "Execution requires --execute and matching live-qualification evidence."}
    if readiness is None:
        raise BenchError("No live-qualification receipt supplied; no model calls made")
    selected = {**manifest, "cells": cells,
                "configurations": [v for k, v in configurations.items() if configuration_id is None or k == configuration_id]}
    validate_readiness(selected, json.loads(Path(readiness).read_text()), slots=slots)
    if manifest["track"] == "vast_self_hosted" and configuration_id is None:
        raise BenchError("Select the single self-hosted configuration loaded on the endpoint")
    if manifest["track"] == "vast_self_hosted":
        if endpoint is None:
            raise BenchError("A pinned inference endpoint is required")
        validate_endpoint(configurations[configuration_id], endpoint)
    state_name = manifest["protocol_sha256"] + "-" + manifest["track"]
    progress_path = store.root / "studies" / (state_name + "-progress.jsonl")
    lock_fd = os.open(store.root / "studies" / (state_name + ".lock"), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    cancelled = threading.Event()
    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise BenchError("This protocol track already has a runner") from exc
        dataset = Dataset(store)
        reconcile_attempts(store, manifest, progress_path, dataset)
        history = read_events(progress_path)
        finished = {r["logical_trial_id"] for r in history if r["status"] == "completed"}
        pending = [cell for cell in cells if cell["logical_trial_id"] not in finished]
        batch_path = private_dir(store.root / "runs" / ("batch-" + uuid.uuid4().hex[:12]))
        timings = Timings(batch_path / "timings.jsonl", track=manifest["track"])
        completed = []
        failed = []
        batch_status = "completed"

        def persist(future):
            path, result = future.result()
            if result["attempt_id"] in completed:
                return
            # Dataset first; reconciliation makes either crash boundary idempotent.
            dataset.ingest(path)
            append_event(progress_path, progress_record(path, result), unique_key="attempt_id")
            completed.append(result["attempt_id"])
            if result["status"] != "completed":
                failed.append(result["attempt_id"])
            progress(f"{len(completed)}/{len(pending)}: {result['configuration_id']} {result['task']} {result['route']} {result['status']}")

        with timings.span("batch_makespan"):
            executor = ThreadPoolExecutor(max_workers=slots)
            futures = []
            try:
                futures = [executor.submit(run_attempt, store, protocol, stack, manifest, cell,
                                           cancelled=cancelled, endpoint=endpoint) for cell in pending]
                for future in as_completed(futures):
                    persist(future)
            except KeyboardInterrupt:
                batch_status = "interrupted"
                cancelled.set()
                for future in futures:
                    future.cancel()
            except BaseException:
                cancelled.set()
                for future in futures:
                    future.cancel()
                raise
            finally:
                executor.shutdown(wait=True, cancel_futures=True)
                for future in futures:
                    if not future.cancelled() and future.done() and future.exception() is None:
                        persist(future)
        if failed and batch_status == "completed":
            batch_status = "needs_recovery"
        summary = {"status": batch_status, "track": manifest["track"], "attempts": completed,
                   "invalid_or_interrupted_attempts": failed, "previously_completed": len(cells) - len(pending),
                   "report": dataset.report()}
        atomic_json(batch_path / "result.json", summary)
        finalize(batch_path)
        return summary
    finally:
        cancelled.set()
        os.close(lock_fd)


def progress_record(path, result):
    return {"logical_trial_id": result["logical_trial_id"], "attempt_id": result["attempt_id"],
            "status": result["status"], "path": str(path)}


def reconcile_attempts(store, manifest, progress_path, dataset):
    """A crash after finalization must not silently repeat a completed trial."""
    cells = {c["logical_trial_id"]: c for c in manifest["cells"]}
    for result_file in sorted((store.root / "runs").glob("attempt-*/result.json")):
        result = json.loads(result_file.read_text())
        if result.get("run_kind") != "scored" or result.get("protocol_sha256") != manifest["protocol_sha256"] or result.get("track") != manifest["track"]:
            continue
        cell = cells.get(result.get("logical_trial_id"))
        if cell is None:
            continue
        path = result_file.parent
        if any(result.get(k) != v for k, v in cell.items()):
            raise BenchError("Attempt provenance differs from the frozen logical trial")
        if not (path / "checksums.json").exists():
            raise BenchError("Recover the interrupted attempt before resuming: " + str(path))
        evidence = verify_evidence(path)
        if result.get("cleanup") == "failed_or_unknown":
            receipt = store.root / "reports" / ("recovery-" + result["attempt_id"] + ".json")
            if not receipt.exists() or json.loads(receipt.read_text()).get("evidence_sha256") != evidence:
                raise BenchError("Recover the recorded VM before resuming: " + str(path))
        dataset.ingest(path)
        append_event(progress_path, progress_record(path, result), unique_key="attempt_id")


def recover_attempt(store, path):
    """Reconcile one recorded owned VM; never infer ownership from a process name."""
    path = Path(path).resolve()
    if not path.is_relative_to(store.root / "runs"):
        raise BenchError("Recovery is limited to this runtime store's attempt records")
    finalized = (path / "checksums.json").exists()
    evidence = verify_evidence(path) if finalized else None
    result = json.loads((path / "result.json").read_text())
    if result.get("schema") != "kilix-agent-bench/attempt-v1":
        raise BenchError("Not an owned attempt record")
    if result.get("vm_id"):
        directory = store.vm_dir(result["vm_id"])
        if directory.exists():
            VM(store, result["vm_id"]).destroy()
    if finalized:
        receipt = {"status": "recovered", "attempt_id": result["attempt_id"], "cleanup": "passed",
                   "evidence_sha256": evidence, "completed_at": timestamp()}
        atomic_json(store.root / "reports" / ("recovery-" + result["attempt_id"] + ".json"), receipt)
        return receipt
    result.update(status="interrupted", cleanup="passed", completed_at=timestamp(),
                  outcome={"outcome": "invalid_infrastructure", "eligible_success": False})
    atomic_json(path / "result.json", result)
    finalize(path)
    return {"status": "recovered", "attempt_id": result["attempt_id"], "cleanup": "passed"}
