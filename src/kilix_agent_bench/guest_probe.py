"""Root-owned observer for the canonical Kilix stack, executed only in a VM.

REQUEST is supplied by the host. Child coding clients are explicit fixtures;
this checks real pane/grid transport without authenticating or calling models.
"""
import base64
import hashlib
import json
import os
from pathlib import Path
import pwd
import signal
import stat
import subprocess
import time


def probe(request):
    user = pwd.getpwnam("actor")
    home = Path(user.pw_dir)
    root = home / ".local/gpu_terminal"
    work = home / "stack-probe"
    work.mkdir(mode=0o700)
    os.chown(work, user.pw_uid, user.pw_gid)
    fixtures = work / "bin"
    fixtures.mkdir(mode=0o700)
    os.chown(fixtures, user.pw_uid, user.pw_gid)
    runtime = work / "runtime"
    runtime.mkdir(mode=0o700)
    os.chown(runtime, user.pw_uid, user.pw_gid)
    socket = "unix:" + str(work / "engine.sock")
    environment = {"HOME": str(home), "USER": "actor", "LOGNAME": "actor", "LANG": "C.UTF-8",
                   "PATH": str(fixtures) + ":" + str(home / ".local/bin") + ":/usr/bin:/bin",
                   "DISPLAY": ":91", "XDG_RUNTIME_DIR": str(runtime), "LIBGL_ALWAYS_SOFTWARE": "1",
                   "KITTY_LISTEN_ON": socket, "KILIX_APP_AUTO_INSTALL": "0"}
    prefix = ["runuser", "-u", "actor", "--", "env", "-i"] + [k + "=" + v for k, v in environment.items()]
    record = {"checks": {}, "observations": {}, "commands": [], "provider_clients": "deterministic-fixtures",
              "controller_model_calls": 0, "local_model_inference_calls": 0}
    processes = []

    def write(name, data, mode=0o600):
        path = work / name
        path.write_bytes(data)
        path.chmod(mode)
        os.chown(path, user.pw_uid, user.pw_gid)
        return path

    def execute(argv, timeout=40, check=True):
        p = subprocess.run(prefix + argv, cwd=work, capture_output=True, timeout=timeout)
        record["commands"].append({"argv": argv, "exit": p.returncode,
                                   "stdout": p.stdout.decode(errors="replace")[-6000:],
                                   "stderr": p.stderr.decode(errors="replace")[-2000:]})
        if check and p.returncode:
            raise RuntimeError(f"Command failed ({p.returncode}): {argv}: {p.stderr.decode(errors='replace')[-1200:]}")
        return p

    def launch(argv, label):
        with (Path("/var/lib/bench-stack") / (label + ".log")).open("wb") as log:
            p = subprocess.Popen(prefix + argv, cwd=work, stdin=subprocess.DEVNULL,
                                 stdout=log, stderr=log, start_new_session=True)
        processes.append(p)
        return p

    def wait_for(test, description, seconds=15):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            value = test()
            if value:
                return value
            time.sleep(0.2)
        raise RuntimeError("Timed out waiting for " + description)

    def observe_bytes(path):
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size > 65536:
                raise RuntimeError("Invalid observer file")
            return os.read(fd, 65537)
        finally:
            os.close(fd)

    engine = root / "kilix/build/current/src/kitty/launcher/kitty"
    kitten = engine.with_name("kitten")
    def remote(*args, check=True):
        return execute([str(kitten), "@", "--to", socket, *args], check=check)
    def panes():
        data = json.loads(remote("ls").stdout)
        return {p["id"]: {"tab_id": tab["id"], **p}
                for window in data for tab in window["tabs"] for p in tab["windows"]}

    try:
        sources = []
        for source in request["sources"]:
            directory = root / source["destination"]
            head = execute(["git", "-C", str(directory), "rev-parse", "HEAD"]).stdout.decode().strip()
            clean = execute(["git", "-C", str(directory), "diff", "--quiet", "HEAD"], check=False).returncode == 0
            origin = execute(["git", "-C", str(directory), "remote", "get-url", "origin"]).stdout.decode().strip()
            sources.append({"destination": source["destination"], "commit": head, "clean": clean, "origin": origin})
            if head != source["commit"] or not clean or origin != source["repository"]:
                raise RuntimeError("Installed source does not match its lock")
        record["observations"]["sources"] = sources
        record["checks"]["sources_match"] = True
        engine_hash = hashlib.sha256(engine.read_bytes()).hexdigest()
        record["observations"]["engine_sha256"] = engine_hash
        record["checks"]["engine_bytes_match"] = engine_hash == request["engine_sha256"]
        source_id = (root / "kilix/build/current/source-id").read_text().strip()
        engine_commit = next(s["commit"] for s in request["sources"] if s["destination"] == "sources/kilix/src")
        record["checks"]["engine_source_matches_launcher"] = source_id == engine_commit
        selected = execute(["kilix", "--which"]).stdout.decode().splitlines()
        record["checks"]["launcher_selects_frozen_engine"] = selected[0] == str(engine)
        record["observations"]["engine_version"] = execute([str(engine), "--version"]).stdout.decode().strip()
        execute([str(engine), "+runpy", "import kitty.fast_data_types as f; assert f.wcswidth('a') == 1"])
        record["checks"]["engine_extension_loads"] = True
        for backend in ("lfm2", "grammar"):
            result = json.loads(execute(["kilix-needle", "workflows", "status", "--model", backend,
                                        "--root", str(root / "kilix/data/desktop-apps"), "--json"], timeout=90).stdout)
            record["observations"]["workflows_" + backend] = result
            record["checks"][backend + "_assets_ready"] = result.get("ready") is True
        wrapper = home / ".local/bin/kilix-rollout-resume"
        before = hashlib.sha256(wrapper.read_bytes()).hexdigest()
        for _ in range(2):
            execute(["kilix", "rollout", "log", "--help"])
        record["checks"]["rollout_log_does_not_reinstall"] = before == hashlib.sha256(wrapper.read_bytes()).hexdigest()
        record["observations"]["grammar_memory"] = execute(["kilix", "grammar", "show memory"]).stdout.decode()
        record["checks"]["system_grammar_executes"] = True

        fixture = b'''#!/usr/bin/python3
import json, os, pathlib, sys, time
root = pathlib.Path.home() / 'stack-probe'
(root / ('launch-' + str(os.getpid()) + '.json')).write_text(json.dumps({'argv': sys.argv[1:], 'cwd': os.getcwd()}))
print('BENCH_CLIENT_FIXTURE_READY', flush=True)
time.sleep(300)
'''
        write("bin/codex", fixture, 0o700)
        launch(["Xvfb", ":91", "-screen", "0", "1280x800x24", "-nolisten", "tcp", "-noreset"], "xvfb")
        wait_for(lambda: Path("/tmp/.X11-unix/X91").exists(), "guest X11")
        launch(["kilix", "--config", "NONE", "--listen-on", socket,
                "-o", "allow_remote_control=yes", "-o", "enabled_layouts=tall,fat,grid,splits,sessions",
                "--title", "bench-unrelated", "/bin/sleep", "300"], "engine")
        wait_for(lambda: (work / "engine.sock").exists(), "guest engine socket")
        wait_for(lambda: remote("ls", check=False).returncode == 0, "engine remote control")
        baseline = panes()
        record["observations"]["baseline_pane_ids"] = sorted(baseline)
        caller = next(iter(baseline.values()))
        context = caller["env"]
        if context.get("KITTY_LISTEN_ON") != socket or not context.get("KITTY_PUBLIC_KEY"):
            raise RuntimeError("Owned initial pane has no valid controller context")
        for key in ("KITTY_PID", "KITTY_WINDOW_ID", "KITTY_PUBLIC_KEY", "KILIX_RC_PASSWORD_FILE",
                    "KITTY_PTY_BROKER_EXECUTABLE", "KITTY_PTY_BROKER_RUNTIME", "KITTY_PTY_BROKER_SESSION"):
            if key in context:
                environment[key] = context[key]
        prefix[:] = ["runuser", "-u", "actor", "--", "env", "-i"] + [k + "=" + v for k, v in environment.items()]
        record["checks"]["controller_context_bound"] = True

        copy_code = "import pathlib,sys,time; print('BENCH_LITERAL_READY',flush=True); data=sys.stdin.buffer.readline(); pathlib.Path(sys.argv[1]).write_bytes(data.removesuffix(b'\\n')); time.sleep(300)"
        result_path = work / "literal-result"
        copy_id = int(remote("launch", "--type=tab", "--title=bench-literal", "--cwd=" + str(work),
                             "python3", "-c", copy_code, str(result_path)).stdout)
        wait_for(lambda: b"BENCH_LITERAL_READY" in remote("get-text", "--match", "id:" + str(copy_id)).stdout,
                 "literal reader")
        execute(["kilix", "pane", "send", str(copy_id), request["literal"]])
        record["checks"]["typing_does_not_submit"] = not result_path.exists()
        remote("send-text", "--match", "id:" + str(copy_id), "\r")
        wait_for(result_path.exists, "submitted literal bytes")
        record["observations"]["literal_base64"] = base64.b64encode(observe_bytes(result_path)).decode()

        before = panes()
        route_result = json.loads(execute(["kilix", "claudex", "route", "worker", "low", "--provider", "codex", "--json"], timeout=90).stdout)
        wait_for(lambda: len(list(work.glob("launch-*.json"))) == 1, "worker process fixture")
        after = panes()
        launched = [json.loads(observe_bytes(p)) for p in work.glob("launch-*.json")]
        record["observations"]["worker"] = {"receipt": route_result, "processes": launched}
        record["checks"]["worker_opens_one_pane"] = len(set(after) - set(before)) == 1
        record["checks"]["worker_preserves_model_and_max"] = launched[0]["argv"] == ["--yolo", "-m", "gpt-6-luna", "-c", "model_reasoning_effort=max"]

        prompt = "--literal prompt $HOME ; λ"
        plan = {"name": "bench-team", "defaults": {"provider": "codex", "model": "gpt-6.1-terra", "effort": "high",
                 "cwd": str(work), "prompt": prompt},
                "sessions": [{"title": "bench-team-one"}, {"title": "bench-team-two", "model": "gpt-6.1-sol", "effort": "low"}]}
        plan_path = write("team.json", json.dumps(plan).encode())
        before = panes()
        team_result = json.loads(execute(["kilix", "claudex", "team", "launch", str(plan_path), "--json"], timeout=90).stdout)
        wait_for(lambda: len(list(work.glob("launch-*.json"))) == 3, "team process fixtures")
        after = panes()
        new_ids = set(after) - set(before)
        launched = [json.loads(observe_bytes(p)) for p in work.glob("launch-*.json")]
        record["observations"]["team"] = {"receipt": team_result, "processes": launched, "pane_ids": sorted(new_ids)}
        record["checks"]["team_opens_two_in_one_tab"] = len(new_ids) == 2 and len({after[i]["tab_id"] for i in new_ids}) == 1
        record["checks"]["team_preserves_prompts"] = sum(p["argv"][-2:] == ["--", prompt] for p in launched) == 2
        expected_team = {("--yolo", "-m", model, "-c", "model_reasoning_effort=" + effort, "--", prompt)
                         for model, effort in (("gpt-6.1-terra", "high"), ("gpt-6.1-sol", "low"))}
        record["checks"]["team_preserves_models_and_efforts"] = expected_team.issubset({tuple(p["argv"]) for p in launched})
        record["checks"]["worker_and_team_cwd_preserved"] = all(p["cwd"] == str(work) for p in launched)
        record["checks"]["unrelated_panes_preserved"] = all(i in after and after[i]["pid"] == baseline[i]["pid"] for i in baseline)
    except BaseException as exc:
        record["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        for p in reversed(processes):
            if p.poll() is None:
                try:
                    os.killpg(p.pid, signal.SIGTERM)
                    p.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(p.pid, signal.SIGKILL)
                    p.wait(timeout=5)
        record["observer_completed"] = True
    return record


if __name__ == "__main__":
    print(json.dumps(probe(REQUEST)))
