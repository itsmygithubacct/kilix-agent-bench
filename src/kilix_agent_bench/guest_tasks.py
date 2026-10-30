"""Trusted fixture/observer entry point. This file runs only inside the owned VM."""
import base64
import hashlib
import json
import os
from pathlib import Path
import pwd
import resource
import shutil
import signal
import stat
import subprocess
import tempfile
import time


STATE = Path("/var/lib/bench-task")


def read_json(path):
    return json.loads(Path(path).read_text())


def save(value):
    (STATE / "state.json").write_text(json.dumps(value))


def bounded(path, limit=1048576):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise RuntimeError("Invalid fixture observation")
        value = os.read(fd, limit + 1)
        if len(value) > limit:
            raise RuntimeError("Fixture grew beyond observation bound")
        return value
    finally:
        os.close(fd)


def prefix(state):
    return ["runuser", "-u", "actor", "--", "env", "-i"] + [k + "=" + v for k, v in state["env"].items()]


def run(state, argv, *, timeout=20, check=True):
    p = subprocess.run(prefix(state) + argv, cwd=state["cwd"], capture_output=True, timeout=timeout)
    if check and p.returncode:
        raise RuntimeError(f"Fixture command failed: {argv!r}: {p.stderr.decode(errors='replace')[-1500:]}")
    return p


def remote(state, *args, check=True):
    return run(state, [state["kitten"], "@", "--to", state["env"]["KITTY_LISTEN_ON"], *args], check=check)


def panes(state):
    data = json.loads(remote(state, "ls", "--all-env-vars").stdout)
    return {str(p["id"]): {"tab_id": tab["id"], **p} for window in data for tab in window["tabs"] for p in tab["windows"]}


def wait_for(test, label, seconds=20):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        value = test()
        if value:
            return value
        time.sleep(0.05)
    raise RuntimeError("Timed out waiting for " + label)


def start_process(state, argv, label):
    with (STATE / (label + ".log")).open("wb") as log:
        p = subprocess.Popen(prefix(state) + argv, cwd=state["cwd"], stdin=subprocess.DEVNULL,
                             stdout=log, stderr=log, start_new_session=True)
    state["process_groups"].append(p.pid)
    save(state)


def setup(spec):
    if STATE.exists():
        raise RuntimeError("Task state already exists; reset or clean the previous fixture")
    STATE.mkdir(mode=0o700)
    user = pwd.getpwnam("actor")
    home = Path(user.pw_dir)
    work = home / "bench-work"
    work.mkdir(mode=0o700)
    os.chown(work, user.pw_uid, user.pw_gid)
    runtime = work / "runtime"
    runtime.mkdir(mode=0o700)
    os.chown(runtime, user.pw_uid, user.pw_gid)
    root = home / ".local/gpu_terminal"
    bin_dir = Path("/opt/bench-fixtures")
    bin_dir.mkdir(mode=0o755, exist_ok=True)
    engine = root / "kilix/build/current/src/kitty/launcher/kitty"
    kitten = engine.with_name("kitten")
    (bin_dir / "kitten").symlink_to(kitten)
    (work / "unrelated.txt").write_text("unrelated fixture state\n")
    os.chown(work / "unrelated.txt", user.pw_uid, user.pw_gid)
    state = {"spec": spec, "cwd": str(work), "kitten": str(kitten), "process_groups": [],
             "protected_paths": [], "baseline": {}, "env": {
        "HOME": str(home), "USER": "actor", "LOGNAME": "actor", "LANG": "C.UTF-8",
        "PATH": str(bin_dir) + ":" + str(home / ".local/bin") + ":/usr/bin:/bin",
        "DISPLAY": ":92", "XDG_RUNTIME_DIR": str(runtime), "LIBGL_ALWAYS_SOFTWARE": "1",
        "KILIX_CONTENT_ROOT": str(root / "kilix/data/desktop-apps"),
        "KILIX_DATA_HOME": str(root / "kilix/data"),
        "KITTY_LISTEN_ON": "unix:" + str(work / "engine.sock"), "KILIX_APP_AUTO_INSTALL": "0"}}
    save(state)

    def fixture_file(path, content, *, writable=False, executable=False):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        path.chmod(0o755 if executable else (0o600 if writable else 0o444))
        if writable:
            os.chown(path, user.pw_uid, user.pw_gid)
        else:
            state["protected_paths"].append(str(path))
        return path

    worker_source = '''#!/usr/bin/python3
import json, os, pathlib, sys, time
if '--help' in sys.argv or '-h' in sys.argv:
 print('Usage: codex [--yolo] [-m MODEL] [-c SETTING]'); raise SystemExit(0)
if '--version' in sys.argv:
 print('bench-process-fixture-v1'); raise SystemExit(0)
args=sys.argv[1:]
model=None
effort='high'
for i,arg in enumerate(args):
 if arg in ('-m','--model') and i+1<len(args): model=args[i+1]
 if arg in ('-c','--config') and i+1<len(args) and args[i+1].startswith('model_reasoning_effort='):
  effort=args[i+1].split('=',1)[1].strip('"')
record={'pid':os.getpid(),'argv':args,'cwd':os.getcwd(),'model':model,'effort':effort,'ready':True}
p=pathlib.Path.home()/'bench-work'/('worker-'+str(os.getpid())+'.json')
p.write_text(json.dumps(record))
print('BENCH_WORKER_READY',flush=True)
time.sleep(3600)
'''
    fixture_file(bin_dir / "codex", worker_source.encode(), executable=True)
    search = work / "search tree"
    search.mkdir()
    files = {"one space.txt": "header\n" + spec["literal"] + "\ntail\n",
             "sub/λ ; quote.txt": spec["literal"] + "\nagain " + spec["literal"] + "\n",
             "none.txt": "no matches here\n", ".hidden": spec["literal"] + "\n"}
    for name, content in files.items():
        fixture_file(search / name, content.encode())
    fixture_file(work / "outside-decoy.txt", spec["literal"].encode())
    (search / "link.txt").symlink_to(work / "outside-decoy.txt")
    state["search_root"] = str(search)
    state["expected_matches"] = [{"path": "one space.txt", "line": 2}, {"path": "sub/λ ; quote.txt", "line": 1}]
    state["ram_bytes"] = int(next(line.split()[1] for line in Path("/proc/meminfo").read_text().splitlines() if line.startswith("MemTotal:"))) * 1024

    start_process(state, ["Xvfb", ":92", "-screen", "0", "1280x800x24", "-nolisten", "tcp", "-noreset"], "xvfb")
    wait_for(lambda: Path("/tmp/.X11-unix/X92").exists(), "X11")
    start_process(state, ["kilix", "--config", "NONE", "--listen-on", state["env"]["KITTY_LISTEN_ON"],
                         "-o", "allow_remote_control=yes", "-o", "enabled_layouts=tall,fat,grid,splits,sessions",
                         "--title", "bench-caller", "/bin/sleep", "3600"], "engine")
    wait_for(lambda: (work / "engine.sock").exists(), "terminal socket")
    wait_for(lambda: remote(state, "ls", check=False).returncode == 0, "terminal connection")
    def ready_caller():
        candidates = panes(state)
        for pane in candidates.values():
            env = pane.get("env", {})
            if env.get("KITTY_PUBLIC_KEY") and env.get("KILIX_RC_PASSWORD_FILE") and env.get("KITTY_WINDOW_ID"):
                return pane
        return None
    caller = wait_for(ready_caller, "complete caller connection metadata")
    state["caller_pane"] = caller["id"]
    for key in ("KITTY_PID", "KITTY_WINDOW_ID", "KITTY_PUBLIC_KEY", "KILIX_RC_PASSWORD_FILE",
                "KITTY_PTY_BROKER_EXECUTABLE", "KITTY_PTY_BROKER_RUNTIME", "KITTY_PTY_BROKER_SESSION"):
        if key in caller["env"]:
            state["env"][key] = caller["env"][key]
    state["target_pane"] = state["caller_pane"]
    recorder = '''#!/usr/bin/python3
import os, pathlib, sys, termios, tty
tty.setraw(0)
path=pathlib.Path(sys.argv[1])
path.write_bytes(b'')
if len(sys.argv)>2:
 os.write(1, ('SCROLLBACK_DECOY\\r\\n'*100 + '\\x1b[2J\\x1b[H' + sys.argv[2] + '\\r\\n').encode())
else: os.write(1,b'BENCH_INPUT_READY\\r\\n')
with path.open('ab',buffering=0) as output:
 while True:
  data=os.read(0,4096)
  if not data: break
  output.write(data)
'''
    recorder_path = fixture_file(bin_dir / "input-recorder", recorder.encode(), executable=True)
    if spec["task"] in {"pane_literal_without_submit", "pane_visible_text_read"}:
        argv = [str(recorder_path), str(work / "input-bytes")]
        if spec["task"] == "pane_visible_text_read":
            argv.append(spec["screen_text"])
        target = remote(state, "launch", "--type=tab", "--title=bench-target", "--cwd=" + str(work), *argv)
        state["target_pane"] = int(target.stdout)
        marker = spec["screen_text"] if spec["task"] == "pane_visible_text_read" else "BENCH_INPUT_READY"
        wait_for(lambda: marker.encode() in remote(state, "get-text", "--match", "id:" + str(state["target_pane"])).stdout, "target recorder")
    if spec["task"] == "active_session_log_retrieval":
        logdir = home / ".codex/sessions/2026/01/01"
        logpath = logdir / ("rollout-2026-01-01T00-00-00-" + spec["literal"] + ".jsonl")
        records = [
            {"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "OLD_ASSISTANT"}]}},
            {"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": spec["log_text"]}]}},
            {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "LATEST_USER_DECOY"}]}},
        ]
        fixture_file(logpath, ("\n".join(json.dumps(x) for x in records) + "\n").encode())
        fixture_file(logdir / "rollout-2099-01-01T00-00-00-decoy.jsonl", b'{"text":"STALE_LOG_DECOY"}\n')
        log_source = '''#!/usr/bin/python3
import pathlib,sys,time
log=pathlib.Path(sys.argv[1]).open('rb')
print('TERMINAL_TRANSCRIPT_DECOY',flush=True)
time.sleep(3600)
'''
        live = fixture_file(bin_dir / "codex-log-fixture", log_source.encode(), executable=True)
        state["target_pane"] = int(remote(state, "launch", "--type=tab", "--title=bench-log", "--cwd=" + str(work), str(live), str(logpath)).stdout)
        wait_for(lambda: b"TERMINAL_TRANSCRIPT_DECOY" in remote(state, "get-text", "--match", "id:" + str(state["target_pane"])).stdout, "live rollout")
        state["active_log"] = str(logpath)
    state["baseline"] = panes(state)
    save(state)
    observed = observe(state)
    observed.update({k: state[k] for k in ("cwd", "search_root", "caller_pane", "target_pane", "expected_matches", "ram_bytes")})
    if "active_log" in state:
        observed["active_log"] = state["active_log"]
    return observed


def observe(state):
    current = panes(state)
    preserved = all(ident in current and current[ident]["pid"] == value["pid"] for ident, value in state["baseline"].items())
    work = Path(state["cwd"])
    workers = []
    for path in sorted(work.glob("worker-*.json")):
        row = json.loads(bounded(path))
        row["alive"] = Path("/proc", str(row["pid"])).exists()
        workers.append(row)
    hashes = {path: hashlib.sha256(value).hexdigest() if (value := bounded(path)) is not None else None for path in state["protected_paths"]}
    return {"unrelated": base64.b64encode(bounded(work / "unrelated.txt") or b"").decode(),
            "protected_hashes": hashes, "caller_preserved": preserved,
            "new_panes": sorted(set(current) - set(state["baseline"])),
            "pane_ids": sorted(current), "workers": workers,
            "input_bytes_base64": base64.b64encode(bounded(work / "input-bytes") or b"").decode()}


def execute(state, request):
    argv = request["argv"]
    if not isinstance(argv, list) or not argv or any(not isinstance(v, str) or "\0" in v for v in argv):
        raise RuntimeError("Invalid literal argv")
    timeout, output_limit = request["timeout_s"], request["output_limit"]
    cwd = request.get("cwd") or state["cwd"]
    if not Path(cwd).is_absolute() or not Path(cwd).is_dir():
        return {"exit_code": 2, "stdout": "", "stderr": "cwd must be an existing absolute guest directory", "elapsed_s": 0, "timed_out": False}
    trace = STATE / ("exec-" + str(time.monotonic_ns()) + ".log")
    tracer = "/opt/bench-trace"
    if not Path(tracer).is_file():
        raise RuntimeError("Execution audit binary was not provisioned")
    timed_out = False
    started = time.monotonic()
    with tempfile.TemporaryFile(dir=STATE) as stdout, tempfile.TemporaryFile(dir=STATE) as stderr:
        def limits():
            resource.setrlimit(resource.RLIMIT_FSIZE, (1048576, 1048576))
        p = subprocess.Popen([tracer, "-f", "-qq", "-s", "65536", "-e", "trace=execve", "-o", str(trace),
                              *prefix(state), *argv], cwd=cwd, stdin=subprocess.PIPE, stdout=stdout, stderr=stderr,
                             start_new_session=True, preexec_fn=limits)
        try:
            p.communicate(request.get("stdin", "").encode(), timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            os.killpg(p.pid, signal.SIGKILL)
            p.communicate(timeout=5)
        stdout.seek(0)
        stderr.seek(0)
        out, err = stdout.read(1048577), stderr.read(1048577)
    # A launched process is observed after a bounded settling window, never inferred from exit 0.
    time.sleep(0.1)
    return {"exit_code": p.returncode, "stdout": out[:output_limit].decode(errors="replace"),
            "stderr": err[:output_limit].decode(errors="replace"), "stdout_truncated": len(out) > output_limit,
            "stderr_truncated": len(err) > output_limit, "stdout_bytes": len(out), "stderr_bytes": len(err),
            "timed_out": timed_out, "elapsed_s": time.monotonic() - started,
            "exec_audit": (bounded(trace, 1048576) or b"").decode(errors="replace")}


def cleanup(state):
    for pid in reversed(state["process_groups"]):
        try:
            os.killpg(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    time.sleep(0.2)
    # Only owned fixture processes and paths; the scored runner resets the VM afterward.
    for pid in reversed(state["process_groups"]):
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    user = pwd.getpwnam("actor")
    subprocess.run(["pkill", "-u", str(user.pw_uid)], capture_output=True)
    for path in (Path(state["cwd"]), Path("/opt/bench-fixtures"), Path(user.pw_dir) / ".codex", STATE):
        if path.exists():
            shutil.rmtree(path)
    return {"status": "cleaned"}


def dispatch(request):
    if os.geteuid() != 0:
        raise RuntimeError("Fixture observer must run as guest root")
    if request["action"] == "setup":
        return setup(request["scenario"])
    state = read_json(STATE / "state.json")
    if request["action"] == "observe":
        return observe(state)
    if request["action"] == "execute":
        return execute(state, request)
    if request["action"] == "cleanup":
        return cleanup(state)
    raise RuntimeError("Unknown fixture operation")


if __name__ == "__main__":
    import sys
    print(json.dumps(dispatch(json.load(sys.stdin))))
