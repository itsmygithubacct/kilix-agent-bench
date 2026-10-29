"""A real VM lifecycle qualification with no model invocation."""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import sys
import time
import uuid

from . import __version__
from .core import BenchError, atomic_json, digest, now, private_dir
from .tasks import fixture_actor, observe, payload, prepare, verify
from .vm import VM


PROVENANCE = """import hashlib, json, platform, shutil
from pathlib import Path
paths = [Path(shutil.which(name)).resolve() for name in
         ('python3', 'sh', 'cat', 'runuser', 'timeout', 'sshd')]
print(json.dumps({'kernel': platform.release(), 'os_release': Path('/etc/os-release').read_text(),
                  'binaries': [{'path': str(p), 'sha256': hashlib.sha256(p.read_bytes()).hexdigest()} for p in paths]}))
"""


def qualify(store, config, progress=lambda message: None):
    ident = "run-" + uuid.uuid4().hex[:12]
    path = private_dir(store.root / "runs" / ident)
    record = {"schema": "kilix-agent-bench/qualification-v1", "id": ident, "status": "running",
              "started_at": now(), "config": config, "controls": [], "cleanup": "pending",
              "runtime_profile": "base-os-fixture", "kilix_stack_qualified": False,
              "usage": {"model_calls": 0, "measurement": "no_model_invoked"},
              "harness": {"version": __version__, "python_sha256": digest(sys.executable),
                          "source_sha256": {p.name: digest(p) for p in sorted(Path(__file__).parent.glob('*.py'))}}}
    report = path / "result.json"
    atomic_json(report, record)
    vm = None
    started = time.monotonic()
    try:
        progress("Checking prerequisites and acquiring the pinned image")
        vm = VM.create(store, config)
        record["vm_id"] = vm.ident
        atomic_json(report, record)
        progress(f"Booting and provisioning {vm.ident}")
        vm.start()
        record["guest"] = json.loads(vm.ssh(PROVENANCE).stdout)
        record["vm_manifest"] = vm.state.copy()
        atomic_json(report, record)
        for incorrect in (False, True):
            label = "negative" if incorrect else "positive"
            if incorrect:
                progress("Resetting the guest disk before the negative control")
                vm.reset()
                record["reset_generation"] = vm.state["generation"]
            progress(f"Running {label} literal-file control")
            content = payload()
            prepare(vm, content)
            begin = time.monotonic()
            actor = fixture_actor(vm, incorrect=incorrect)
            observation = observe(vm)
            score = verify(observation, content, actor["exit"])
            required = {key: key != "exact_bytes" for key in score["checks"]} if incorrect else {key: True for key in score["checks"]}
            matched = score["checks"] == required
            record["controls"].append({"control": label, "task": "literal_file/v1",
                                       "actor": "deterministic-fixture", "actor_result": actor,
                                       "observation": observation, "score": score,
                                       "expected_outcome": "valid_fail" if incorrect else "valid_pass",
                                       "matched_expectation": matched,
                                       "elapsed_s": time.monotonic() - begin})
            shutil.copyfile(vm.path / "serial.log", path / f"{label}-serial.log")
            atomic_json(report, record)
            if not matched:
                raise BenchError(f"{label} control did not match the independent oracle")
        record["status"] = "passed"
    except BaseException as exc:
        record.update(status="invalid_infrastructure", error=f"{type(exc).__name__}: {exc}")
        if isinstance(exc, KeyboardInterrupt):
            record["status"] = "interrupted"
    finally:
        if vm is not None:
            try:
                progress("Stopping and removing the owned guest")
                if (vm.path / "serial.log").is_file():
                    shutil.copyfile(vm.path / "serial.log", path / "final-serial.log")
                if (vm.path / "qemu.log").is_file():
                    shutil.copyfile(vm.path / "qemu.log", path / "qemu.log")
                record["final_vm_state"] = vm.state.copy()
                vm.destroy()
                record["cleanup"] = "passed"
                record["vm_directory_removed"] = not vm.path.exists()
            except BaseException as exc:
                record.update(cleanup="failed", cleanup_error=f"{type(exc).__name__}: {exc}", status="invalid_infrastructure")
        else:
            record["cleanup"] = "no_guest_started"
        record.update(completed_at=now(), elapsed_s=time.monotonic() - started)
        atomic_json(report, record)
    return {"status": record["status"], "report": str(report), "cleanup": record["cleanup"]}
