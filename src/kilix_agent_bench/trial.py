"""Host-owned trial control. Every model command is dispatched to its VM."""
import json
from pathlib import Path
import re
import subprocess
import time

from .core import BenchError, atomic_json, digest, private_dir
from . import guest_tasks
from .scenarios import admit, score
from .telemetry import append_event, timestamp


class GuestTrial:
    def __init__(self, vm, path, spec, route, timings, *, limits=None):
        self.vm, self.path, self.spec, self.route, self.timings = vm, Path(path), spec, route, timings
        self.limits = limits or {"max_tool_calls": 12, "tool_timeout_s": 90, "tool_output_bytes": 16384}
        self.calls, self.violations, self.primary_seen = [], [], False
        private_dir(self.path / "observer")
        self.before = None

    def rpc(self, request, *, timeout=120):
        wire = json.dumps(request)
        script = "import json,runpy\nentry=runpy.run_path('/opt/bench_tasks.py')\nprint(json.dumps(entry['dispatch'](json.loads(" + repr(wire) + "))))\n"
        response = self.vm.ssh(script, timeout=timeout)
        try:
            return json.loads(response.stdout)
        except ValueError as exc:
            raise BenchError("Invalid guest fixture/observer response") from exc

    def install(self, trace_binary):
        self.vm.upload(Path(guest_tasks.__file__), "/opt/bench_tasks.py")
        self.vm.upload(trace_binary, "/opt/bench-trace")
        self.vm.ssh("import os\nos.chmod('/opt/bench-trace',0o755)\n")
        return {"guest_tasks_sha256": digest(Path(guest_tasks.__file__)), "exec_audit_sha256": digest(trace_binary)}

    def setup(self):
        with self.timings.span("fixture_setup"):
            self.before = self.rpc({"action": "setup", "scenario": self.spec})
        atomic_json(self.path / "observer/before.json", self.before)
        return self.before

    def execute(self, arguments):
        started = timestamp()
        if not isinstance(arguments, dict) or set(arguments) - {"argv", "stdin", "cwd"}:
            return self.refusal(arguments, "Tool accepts argv, optional stdin and optional guest cwd only")
        argv = arguments.get("argv")
        if not admit(self.route, argv):
            return self.refusal(arguments, "Command does not match the required route")
        if len(self.calls) >= self.limits["max_tool_calls"]:
            return self.refusal(arguments, "Tool-call budget exhausted")
        if not isinstance(arguments.get("stdin", ""), str) or len(arguments.get("stdin", "").encode()) > 65536:
            return self.refusal(arguments, "stdin must be text bounded to 65536 bytes")
        if arguments.get("cwd") is not None and not isinstance(arguments["cwd"], str):
            return self.refusal(arguments, "cwd must be a guest path")
        with self.timings.span("tool_call", route=self.route, tool_index=len(self.calls)):
            result = self.rpc({"action": "execute", **arguments, "timeout_s": self.limits["tool_timeout_s"],
                               "output_limit": self.limits["tool_output_bytes"]},
                              timeout=self.limits["tool_timeout_s"] + 15)
        audit = result.pop("exec_audit", "")
        if not audit:
            raise BenchError("Missing execution audit")
        if self.route in {"native_shell", "native_log_inspection"}:
            paths = re.findall(r'execve\("([^"\n]*)"', audit)
            if any(Path(p).name in {"kilix", "kilix-needle", "kilix-system", "claudex"} for p in paths):
                self.violations.append("A native-route command invoked a workflow helper")
        help_call = any(arg in ("--help", "-h", "--version") for arg in argv)
        # Route adherence is separate from whether the operation succeeded.
        primary = not help_call
        if self.route == "kilix_rollout_log_cli":
            primary = primary and Path(argv[0]).name == "kilix"
        if self.route == "structured_action_cli":
            primary = primary and argv[1:2] == ["action"] and "--request-json" in argv
        if self.route == "native_session_grid":
            primary = primary and "new-tab" in argv
        if self.route.startswith("ask_") and primary:
            try:
                receipt = json.loads(result["stdout"])
                interpretation = receipt.get("interpretation", "")
                expected = "lfm2" if self.route == "ask_lfm2_model_only_cli" else "grammar"
                primary = interpretation == expected
                result["observed_interpretation"] = interpretation
                if expected == "lfm2" and not primary:
                    self.violations.append("LFM2 inference was not observed")
            except (ValueError, TypeError):
                primary = False
                self.violations.append("Missing backend interpretation receipt")
        self.primary_seen |= primary
        event = {"index": len(self.calls), "started_at": started, "completed_at": timestamp(),
                 "arguments": arguments, "result": result, "exec_audit": audit, "admitted": True}
        self.calls.append(event)
        append_event(self.path / "tools.jsonl", event)
        return result

    def refusal(self, arguments, reason):
        self.violations.append(reason)
        result = {"exit_code": 2, "stdout": "", "stderr": reason, "refused": True}
        event = {"index": len(self.calls), "arguments": arguments, "result": result, "admitted": False, "at": timestamp()}
        self.calls.append(event)
        append_event(self.path / "tools.jsonl", event)
        return result

    def finish(self, answer):
        with self.timings.span("independent_scoring"):
            after = self.rpc({"action": "observe"})
            result = score(self.spec, self.before, after, answer,
                           route_ok=not self.violations and self.primary_seen)
            result["route_violations"] = self.violations.copy()
            result["primary_route_observed"] = self.primary_seen
        atomic_json(self.path / "observer/after.json", after)
        atomic_json(self.path / "outcome.json", result)
        return result

    def cleanup_fixture(self):
        with self.timings.span("fixture_cleanup"):
            return self.rpc({"action": "cleanup"})


def check_client_isolation():
    """Kernel namespace support is a prerequisite, not a silent best-effort mode."""
    result = subprocess.run(["bwrap", "--ro-bind", "/usr", "/usr", "--symlink", "usr/bin", "/bin",
                             "--symlink", "usr/lib", "/lib", "--symlink", "usr/lib64", "/lib64",
                             "--unshare-user", "--unshare-pid", "--proc", "/proc", "--dev", "/dev",
                             "--die-with-parent", "/bin/true"], capture_output=True, timeout=10)
    if result.returncode:
        raise BenchError("Client process isolation is unavailable: " + result.stderr.decode(errors="replace")[-1000:])
    return {"bubblewrap": "passed", "network": "provider connection retained; actions use guest RPC"}
