"""Deterministic positive/negative controls for the selected OS routes."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import re
import time
import uuid

from .core import BenchError, atomic_json, digest, private_dir
from .dataset import finalize
from .protocol import harness_hashes
from .scenarios import TASK_ROUTES, case
from .stack import load_lock, provision
from .telemetry import Timings, timestamp
from .trial import GuestTrial
from .vm import VM


def positive_actor(trial):
    """Qualification actor only. These solved commands are never model context."""
    spec, before, route = trial.spec, trial.before, trial.route
    task = spec["task"]

    def call(argv, stdin="", *, allow_pending_launch=False):
        result = trial.execute({"argv": argv, "stdin": stdin})
        if result["exit_code"] != 0:
            accepted = False
            if allow_pending_launch:
                try:
                    receipt = json.loads(result["stdout"])
                    accepted = any(item.get("receipt", {}).get("pane_created_verified") is True for item in receipt.get("items", []))
                except ValueError:
                    pass
            if not accepted:
                raise BenchError("Qualification command failed: " + result["stderr"][-1000:])
        return result["stdout"]

    if task == "files_literal_search":
        if route == "native_shell":
            script = '''import json,os,pathlib,sys
root=pathlib.Path(sys.argv[1]); needle=sys.argv[2]; found=[]
for parent,dirs,files in os.walk(root,followlinks=False):
 dirs[:]=[d for d in dirs if not d.startswith('.') and not (pathlib.Path(parent)/d).is_symlink()]
 for name in files:
  path=pathlib.Path(parent)/name
  if name.startswith('.') or path.is_symlink() or not path.is_file(): continue
  for line,text in enumerate(path.read_text().splitlines(),1):
   if needle in text: found.append({'path':str(path.relative_to(root)),'line':line}); break
print(json.dumps({'matches':found}))
'''
            return call(["python3", "-c", script, before["search_root"], spec["literal"]]).strip()
        result = json.loads(call(["kilix-needle", "files", "--json", f'find text "{spec["literal"]}" in "{before["search_root"]}"']))
        observation = result["observation"]
        if not observation["complete"] or observation["results_truncated"]:
            raise BenchError("File route did not provide complete coverage")
        return json.dumps({"matches": [{"path": item["relative_path"], "line": item["line"]} for item in observation["results"]]})
    if task == "pane_literal_without_submit":
        argv = (["kitten", "@", "send-text", "--match", "id:" + str(before["target_pane"]), spec["input_text"]]
                if route == "native_remote_control" else ["kilix", "pane", "send", str(before["target_pane"]), spec["input_text"]])
        call(argv)
        return '{"done":true}'
    if task == "system_ram_total":
        if route == "native_shell":
            script = "import json,pathlib; n=int(next(x.split()[1] for x in pathlib.Path('/proc/meminfo').read_text().splitlines() if x.startswith('MemTotal:'))); print(json.dumps({'total':n,'unit':'kB'}))"
            return call(["python3", "-c", script]).strip()
        if route == "kilix_system_cli":
            amount = json.loads(call(["kilix-system", "--json"]))["memory"]["total"]
        else:
            amount = json.loads(call(["kilix", "grammar", "show memory"]))["items"][0]["observation"]["data"]["memory_bytes"]["MemTotal"]
        return json.dumps({"total": amount, "unit": "bytes"})
    if task == "active_session_log_retrieval":
        if route == "kilix_rollout_log_cli":
            record = json.loads(call(["kilix", "rollout", "log", "--pane", str(before["target_pane"]), "--json"]))
            path = record["path"]
        else:
            script = '''import json,os,pathlib,subprocess,sys
windows=json.loads(subprocess.check_output(['kitten','@','ls']))
pane=next(p for w in windows for t in w['tabs'] for p in t['windows'] if p['id']==int(sys.argv[1]))
roots={pane['pid']}
for p in pane.get('foreground_processes',[]): roots.add(p['pid'])
children={}
for p in pathlib.Path('/proc').iterdir():
 if not p.name.isdecimal(): continue
 try:
  fields=(p/'stat').read_text().rsplit(')',1)[1].split(); children.setdefault(int(fields[1]),[]).append(int(p.name))
 except (OSError,ValueError): pass
pending=list(roots)
while pending:
 for child in children.get(pending.pop(),[]):
  if child not in roots: roots.add(child); pending.append(child)
paths=set()
for pid in roots:
 try:
  for fd in pathlib.Path('/proc',str(pid),'fd').iterdir():
   try:
    target=os.readlink(fd)
    if pathlib.Path(target).name.startswith('rollout-') and target.endswith('.jsonl'): paths.add(target)
   except OSError: pass
 except OSError: pass
assert len(paths)==1,paths
print(json.dumps({'path':paths.pop()}))
'''
            path = json.loads(call(["python3", "-c", script, str(before["target_pane"])]))["path"]
        script = '''import json,pathlib,sys
path=pathlib.Path(sys.argv[1]); data=path.read_bytes(); assert len(data)<=65536
messages=[x['payload'] for line in data.splitlines() if (x:=json.loads(line)).get('type')=='response_item' and x.get('payload',{}).get('role')=='assistant']
text=''.join(x['text'] for x in messages[-1]['content'] if x.get('type')=='output_text')
print(json.dumps({'path':str(path),'text':text}))
'''
        return call(["python3", "-c", script, path]).strip()
    if task == "pane_visible_text_read":
        argv = (["kitten", "@", "get-text", "--match", "id:" + str(before["target_pane"]), "--extent", "screen"]
                if route == "native_remote_control" else ["kilix", "watch", "pane:" + str(before["target_pane"]), "--once", "--plain"])
        output = call(argv)
        match = re.search(r"VISIBLE_[0-9a-f]{16}", output)
        if not match:
            raise BenchError("Visible marker absent from route output")
        return json.dumps({"text": match.group()})
    if route in {"native_session_grid", "structured_action_cli"}:
        context = json.loads(call(["kilix", "agent-control", "context"]))["caller"]
        identity = {k: context[k] for k in ("pane_id", "broker")}
        if route == "native_session_grid":
            call(["kilix", "agent-control", "new-tab", str(identity["pane_id"]), "--expect-broker", identity["broker"],
                  "--agent", "codex", "--cwd", before["cwd"], "--title", "bench-worker", "--model", spec["child_model"]])
        else:
            request = {"schema": "kilix.actions/v1", "operation_id": "qualification-" + uuid.uuid4().hex,
                       "operation": "agent.launch", "source": identity, "target": identity,
                       "params": {"agent": "codex", "cwd": before["cwd"], "title": "bench-worker", "placement": "new-tab", "model": spec["child_model"]}}
            call(["kilix", "action", "--request-json", "-"], json.dumps(request))
    elif route == "claudex_worker_explicit":
        call(["claudex", "route", "worker", "high", "--provider", "codex", "--json"])
    else:
        backend = "lfm2" if route == "ask_lfm2_model_only_cli" else "grammar"
        argv = ["kilix-needle", "ask", "--commands-backend", backend, "--yes", "--json"]
        if backend == "lfm2":
            argv.append("--commands-model-only")
        argv.append(f'open codex in a new tab using {spec["child_model"]} in "{before["cwd"]}"')
        call(argv, allow_pending_launch=True)
    return '{"done":true}'


def qualify_routes(store, lock, trace_binary, *, slots=1, cells=None, allow_helper_inference=False, progress=print):
    if slots not in (1, 2):
        raise BenchError("Qualification supports one or two isolated VM slots")
    cells = cells or [(task, route) for task, routes in TASK_ROUTES.items() for route in routes]
    deferred = [(task, route) for task, route in cells if route == "ask_lfm2_model_only_cli" and not allow_helper_inference]
    cells = [cell for cell in cells if cell not in deferred]
    ident = "qualification-" + uuid.uuid4().hex[:12]
    path = private_dir(store.root / "runs" / ident)
    timings = Timings(path / "timings.jsonl", kind="route_qualification")
    _, _, _, config = load_lock(lock)
    record = {"schema": "kilix-agent-bench/route-qualification-v1", "id": ident, "started_at": timestamp(),
              "status": "running", "slots": slots, "controller_model_calls": 0, "cells": [], "vms": [],
              "deferred_inference_cells": deferred, "helper_inference_authorized": allow_helper_inference,
              "stack_lock_sha256": digest(lock), "trace_binary_sha256": digest(trace_binary),
              "harness_sha256": harness_hashes()}
    atomic_json(path / "result.json", record)

    def worker(index, selected):
        vm, results, cleanup = None, [], "not_created"
        local_timing = Timings(path / "timings.jsonl", kind="route_qualification", slot=index)
        try:
            with local_timing.span("vm_create_boot_provision"):
                vm = VM.create(store, config)
                atomic_json(path / f"slot-{index}-owned-vm.json", {"vm_id": vm.ident})
                vm.start()
                provision(vm, lock)
                placeholder = GuestTrial(vm, path, case("system_ram_total", 220200), "native_shell", local_timing)
                placeholder.install(trace_binary)
            for task, route in selected:
                progress(f"Slot {index}: qualifying {task} / {route}")
                controls = []
                for control in ("positive", "negative"):
                    control_path = private_dir(path / f"{task}--{route}--{control}")
                    trial = GuestTrial(vm, control_path, case(task, 220200), route, local_timing)
                    try:
                        trial.setup()
                        answer = positive_actor(trial) if control == "positive" else '{"done":true}'
                        outcome = trial.finish(answer)
                        matched = outcome["eligible_success"] if control == "positive" else outcome["outcome"] == "valid_fail"
                        controls.append({"control": control, "matched": matched, "outcome": outcome, "path": str(control_path)})
                    except Exception as exc:
                        controls.append({"control": control, "matched": False, "error": f"{type(exc).__name__}: {exc}", "path": str(control_path)})
                    finally:
                        try:
                            trial.cleanup_fixture()
                        except Exception as exc:
                            controls.append({"control": "cleanup", "matched": False, "error": str(exc)})
                results.append({"task": task, "route": route, "passed": all(x["matched"] for x in controls), "controls": controls})
                atomic_json(path / f"slot-{index}-partial.json", results)
        finally:
            if vm is not None:
                with local_timing.span("vm_destroy"):
                    vm.destroy()
                cleanup = "destroyed"
        return {"slot": index, "vm_id": vm.ident if vm else None, "cleanup": cleanup, "cells": results}

    began = time.monotonic()
    try:
        with timings.span("qualification_batch"):
            with ThreadPoolExecutor(max_workers=slots) as executor:
                futures = [executor.submit(worker, i, cells[i::slots]) for i in range(slots)]
                for future in futures:
                    result = future.result()
                    record["vms"].append({k: result[k] for k in ("slot", "vm_id", "cleanup")})
                    record["cells"].extend(result["cells"])
        record["status"] = "passed" if len(record["cells"]) == len(cells) and all(c["passed"] for c in record["cells"]) else "failed"
    except BaseException as exc:
        record.update(status="invalid_infrastructure", error=f"{type(exc).__name__}: {exc}")
    finally:
        record.update(completed_at=timestamp(), elapsed_s=time.monotonic() - began)
        atomic_json(path / "result.json", record)
        finalize(path)
    return {"status": record["status"], "report": str(path / "result.json"), "elapsed_s": record["elapsed_s"]}
