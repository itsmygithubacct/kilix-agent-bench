"""Offline driver, failure-recovery and admission tests: no inference or rental."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch, MagicMock

from kilix_agent_bench.bridge import Bridge
from kilix_agent_bench.clients import native_client, self_hosted_client, session_records, stream_process, validate_endpoint
from kilix_agent_bench.core import BenchError, Store, atomic_json, private_dir
from kilix_agent_bench.dataset import Dataset, finalize, verify_evidence
from kilix_agent_bench.protocol import DEFAULT_LIMITS, build_manifest, validate_manifest
from kilix_agent_bench.runner import reconcile_attempts, recover_attempt, run_attempt, run_manifest, slot_lease
from kilix_agent_bench.scenarios import TASK_ROUTES
from kilix_agent_bench.telemetry import Timings, content_hash, read_events
from kilix_agent_bench.usage import normalize_grok


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / "runtime")

    def manifests(self):
        protocol = {"tasks": TASK_ROUTES, "rounds": 5, "development_seeds": list(range(220200, 220205)),
                    "limits": DEFAULT_LIMITS, "vm_config": {}, "trace_binary": "/fixture/strace"}
        configurations = [{"id": "hosted-" + str(i), "model": "fixture-model", "client": "codex", "location": "provider", "effort": "low"} for i in range(10)]
        configurations += [{"id": "local-" + str(i), "model": "local-" + str(i), "profile": "native_primary", "location": "self_hosted"} for i in range(6)]
        local = [{"model": "local-" + str(i), "repository_revision": "a" * 40,
                  "profiles": [{"id": "native_primary", "settings": {"temperature": 0.1}, "precision": "BF16", "context_tokens": 65536}]} for i in range(6)]
        matrix = self.store.root / "studies/matrix.json"
        atomic_json(matrix, {"configurations": configurations, "self_hosted_models": local})
        result = {}
        with patch("kilix_agent_bench.protocol.load_protocol", return_value=(protocol, Path("/fixture/stack"))), patch("kilix_agent_bench.protocol.executable", return_value=Path(sys.executable)):
            for track in ("hosted_api", "vast_self_hosted"):
                for rounds in (1, 5):
                    value = build_manifest(self.store, Path("/fixture/protocol"), matrix, track, rounds=rounds)
                    result[track, rounds] = (Path(value["manifest"]), json.loads(Path(value["manifest"]).read_text()))
        return protocol, result

    def test_manifest_counts_pairing_and_later_model_settings(self):
        protocol, manifests = self.manifests()
        for (track, rounds), (_, manifest) in manifests.items():
            validate_manifest(manifest, protocol)
            self.assertEqual(manifest["scheduled_trials"], (160 if track == "hosted_api" else 96) * rounds)
        first = manifests["hosted_api", 1][1]
        full = manifests["hosted_api", 5][1]
        self.assertEqual(first["cells"], full["cells"][:160])
        for mutation in ("duplicate", "settings", "route", "count"):
            bad = deepcopy(first)
            if mutation == "duplicate": bad["cells"][1] = bad["cells"][0]
            if mutation == "settings": bad["configurations"][0]["effort"] = "high"
            if mutation == "route": bad["cells"][0]["route"] = "unqualified"
            if mutation == "count": bad["scheduled_trials"] = 1
            with self.assertRaises(BenchError): validate_manifest(bad, protocol)

    def test_plan_and_missing_readiness_never_create_vm_or_call_client(self):
        protocol, manifests = self.manifests()
        path, _ = manifests["hosted_api", 1]
        with patch("kilix_agent_bench.runner.load_protocol", return_value=(protocol, Path("/fixture/stack"))), patch("kilix_agent_bench.runner.VM.create") as create:
            self.assertEqual(run_manifest(self.store, path)["trials"], 160)
            with self.assertRaises(BenchError): run_manifest(self.store, path, execute=True)
            create.assert_not_called()

    def test_qualification_driver_cannot_produce_scored_attempt(self):
        with patch("kilix_agent_bench.runner.VM.create") as create:
            with self.assertRaises(BenchError):
                run_attempt(self.store, {}, None, {}, {}, cancelled=threading.Event(), driver=lambda *a: None)
            create.assert_not_called()

    def test_provision_failure_destroys_vm_and_keeps_invalid_evidence(self):
        protocol, manifests = self.manifests()
        manifest = manifests["hosted_api", 1][1]
        vm = MagicMock(); vm.ident = "vm-" + "0" * 12
        with patch("kilix_agent_bench.runner.VM.create", return_value=vm), patch("kilix_agent_bench.runner.provision", side_effect=RuntimeError("injected failure")):
            path, result = run_attempt(self.store, protocol, None, manifest, manifest["cells"][0], cancelled=threading.Event(), run_kind="qualification", driver=lambda *a: None)
        vm.destroy.assert_called_once()
        self.assertEqual(result["status"], "invalid_infrastructure")
        self.assertEqual(result["cleanup"], "passed")
        verify_evidence(path)
        with self.assertRaises(BenchError): Dataset(self.store).ingest(path)

    def test_interrupted_controller_destroys_vm(self):
        protocol, manifests = self.manifests(); manifest = manifests["hosted_api", 1][1]
        vm = MagicMock(); vm.ident = "vm-" + "0" * 12
        trial = MagicMock(); trial.install.return_value = {}; trial.setup.return_value = {}
        with patch("kilix_agent_bench.runner.VM.create", return_value=vm), patch("kilix_agent_bench.runner.provision"), patch("kilix_agent_bench.runner.GuestTrial", return_value=trial), patch("kilix_agent_bench.runner.prompt", return_value="task"):
            path, result = run_attempt(self.store, protocol, None, manifest, manifest["cells"][0], cancelled=threading.Event(), run_kind="qualification", driver=MagicMock(side_effect=InterruptedError("cancelled")))
        vm.destroy.assert_called_once()
        self.assertEqual(result["status"], "interrupted")
        verify_evidence(path)

    def test_crash_between_finalization_and_progress_is_reconciled_once(self):
        _, manifests = self.manifests(); manifest = manifests["hosted_api", 1][1]
        cell = manifest["cells"][0]
        path = private_dir(self.store.root / "runs/attempt-123456789abc")
        result = {"schema": "kilix-agent-bench/attempt-v1", "run_kind": "scored", "attempt_id": path.name,
                  **cell, "track": manifest["track"], "protocol_sha256": manifest["protocol_sha256"],
                  "manifest_sha256": content_hash(manifest), "status": "completed", "cleanup": "passed",
                  "usage": {"ranking_total_tokens": 10}, "outcome": {"outcome": "valid_fail", "eligible_success": False}}
        atomic_json(path / "result.json", result); finalize(path)
        progress = self.store.root / "studies/progress.jsonl"; dataset = Dataset(self.store)
        reconcile_attempts(self.store, manifest, progress, dataset)
        reconcile_attempts(self.store, manifest, progress, dataset)
        self.assertEqual(len(read_events(progress)), 1)
        self.assertEqual(dataset.report()["attempts"], 1)
        (path / "checksums.json").unlink()
        with self.assertRaises(BenchError): reconcile_attempts(self.store, manifest, progress, dataset)
        recover_attempt(self.store, path)
        self.assertEqual(json.loads((path / "result.json").read_text())["status"], "interrupted")

    def test_two_slots_bound_concurrency_and_cancel_waiters(self):
        active = 0; peak = 0; lock = threading.Lock()
        def actor():
            nonlocal active, peak
            with slot_lease(self.store, threading.Event()):
                with lock: active += 1; peak = max(peak, active)
                time.sleep(0.03)
                with lock: active -= 1
        with ThreadPoolExecutor(max_workers=5) as pool:
            list(pool.map(lambda _: actor(), range(5)))
        self.assertEqual(peak, 2)
        cancelled = threading.Event(); cancelled.set()
        with self.assertRaises(InterruptedError):
            with slot_lease(self.store, cancelled): pass

    def test_evidence_covers_nested_checksums_and_rejects_symlink_dirs(self):
        path = private_dir(self.store.root / "runs/evidence"); nested = private_dir(path / "nested")
        atomic_json(nested / "checksums.json", {"value": 1}); finalize(path)
        atomic_json(nested / "checksums.json", {"value": 2})
        with self.assertRaises(BenchError): verify_evidence(path)
        finalize(path); (path / "link").symlink_to(nested, target_is_directory=True)
        with self.assertRaises(BenchError): verify_evidence(path)

    def test_bridge_long_path_proxy_and_request_id_on_error(self):
        deep = private_dir(self.store.root / ("x" * 90)); alias = Path(self.tmp.name) / "short"
        alias.symlink_to(deep, target_is_directory=True)
        def execute(args):
            if args is None: raise ValueError("bad arguments")
            return {"exit_code": 0, "stdout": json.dumps(args)}
        with Bridge(deep / "bridge.sock", execute):
            lines = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26"}},
                     {"jsonrpc": "2.0", "method": "notifications/initialized"},
                     {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                     {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "execute", "arguments": {"argv": ["printf", "λ"]}}},
                     {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "execute"}}]
            completed = subprocess.run([sys.executable, "-m", "kilix_agent_bench.bridge", str(alias / "bridge.sock")], input="".join(json.dumps(v) + "\n" for v in lines), text=True, capture_output=True, timeout=10)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        replies = [json.loads(v) for v in completed.stdout.splitlines()]
        self.assertEqual([r["id"] for r in replies], [1, 2, 3, 4])
        self.assertEqual(replies[2]["result"]["isError"], False)
        self.assertEqual(replies[-1]["error"]["code"], -32603)
        self.assertFalse((deep / "bridge.sock").exists())

    def test_process_timeout_and_cancel_kill_owned_group(self):
        for cancel in (False, True):
            path = private_dir(self.store.root / ("cancelled" if cancel else "timeout"))
            event = threading.Event()
            if cancel: event.set()
            _, code, timed_out = stream_process([sys.executable, "-c", "import time; time.sleep(30)"], dict(os.environ), "", path, 0.05, cancelled=event)
            self.assertTrue(timed_out); self.assertNotEqual(code, 0)


class NativeEvidenceTests(unittest.TestCase):
    def test_live_partial_tail_is_distinct_from_corrupt_final_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "session.jsonl"
            source.write_text('{"type":"assistant"}\n{"type":')
            self.assertEqual(session_records(root, partial=True), [{"type": "assistant"}])
            with self.assertRaises(BenchError): session_records(root)
            source.write_text('broken\n{"type":"assistant"}\n')
            with self.assertRaises(BenchError): session_records(root, partial=True)

    def test_restricted_tools_and_session_metadata_survive_private_home_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = private_dir(root / "operator")
            private_dir(home / ".grok").joinpath("auth.json").write_text("{}")
            path = private_dir(root / "attempt")
            configuration = {"id": "fixture", "client": "grok", "model": "client-model", "effort": "low"}

            def process(command, environment, prompt, output_path, timeout_s, **kwargs):
                self.assertEqual(command[command.index("--tools") + 1], "search_tool,use_tool")
                session = private_dir(path / "client-home/.grok/sessions/fixture")
                atomic_json(session / "summary.json", {"info": {"id": "fixture-session"},
                            "current_model_id": "client-model", "reasoning_effort": "low",
                            "unrelated_account_field": "must-not-copy"})
                atomic_json(session / "usage.json", {"session": {"inputTokens": 20, "outputTokens": 4,
                            "modelCalls": 1, "modelUsage": {"provider-model": {}}}})
                (session / "system_prompt.txt").write_text("fixture policy")
                atomic_json(session / "tool_definitions.json", ["search_tool", "use_tool"])
                return [{"type": "system", "model": "client-model", "session_id": "fixture-session"}], 0, False

            with patch("kilix_agent_bench.clients.Path.home", return_value=home), \
                 patch("kilix_agent_bench.clients.executable", return_value=Path(sys.executable)), \
                 patch("kilix_agent_bench.clients.Bridge"), \
                 patch("kilix_agent_bench.clients.stream_process", side_effect=process):
                result = native_client(configuration, "task", lambda args: {}, path,
                                       Timings(path / "timings.jsonl"), DEFAULT_LIMITS)
            self.assertFalse((path / "client-home").exists())
            self.assertEqual(result["usage"]["observed_efforts"], ["low"])
            self.assertNotIn("unrelated_account_field", json.loads((path / "client-session.json").read_text()))
            self.assertEqual((path / "grok-system_prompt.txt").read_text(), "fixture policy")
            self.assertEqual((path / "client-session.json").stat().st_mode & 0o777, 0o600)


class SelfHostedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)
        self.responses = []; self.requests = []
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_POST(self):
                owner.requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                response = owner.responses.pop(0)
                if response == "redirect":
                    self.send_response(302); self.send_header("Location", "/elsewhere"); self.end_headers(); return
                wire = json.dumps(response).encode()
                self.send_response(200); self.send_header("Content-Length", str(len(wire))); self.end_headers(); self.wfile.write(wire)
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        self.addCleanup(server.server_close); self.addCleanup(server.shutdown)
        self.configuration = {"id": "local", "model": "fixture-model", "repository_revision": "a" * 40, "precision": "BF16", "context_tokens": 65536, "settings": {"temperature": 0.1}}
        self.endpoint = {"url": f"http://127.0.0.1:{server.server_port}/v1", "served_model": "fixture-model",
                         "configuration_sha256": content_hash(self.configuration),
                         "runtime": {"image": "fixture@sha256:" + "0" * 64, "repository_revision": "a" * 40, "precision": "BF16", "context_tokens": 65536, "launch_argv": ["fixture-server"]}}
        self.calls = []

    def response(self, ident, *, calls=None, model="fixture-model", usage=True):
        return {"id": ident, "model": model, "choices": [{"message": {"role": "assistant", "content": '{"done":true}', "tool_calls": calls or []}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 20, "prompt_tokens_details": {"cached_tokens": 30}, "completion_tokens_details": {"reasoning_tokens": 10}} if usage else {}}

    def run_client(self):
        return self_hosted_client(self.configuration, "fixture task", lambda arg: self.calls.append(arg) or {"exit_code": 0, "stdout": "done"}, self.path,
                                  Timings(self.path / "timing.jsonl"), DEFAULT_LIMITS, self.endpoint)

    def test_tool_loop_retains_native_usage_and_provenance(self):
        call = {"id": "tool-1", "type": "function", "function": {"name": "execute", "arguments": '{"argv":["printf","λ"]}'}}
        self.responses = [self.response("r1", calls=[call]), self.response("r2")]
        result = self.run_client()
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["usage"]["ranking_total_tokens"], 240)
        self.assertEqual(result["usage"]["cached_input_tokens"], 60)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.requests[1]["messages"][-1]["role"], "tool")
        self.assertEqual(len(list(self.path.glob("response-*.json"))), 2)

    def test_wrong_model_and_redirect_are_rejected(self):
        self.responses = [self.response("wrong", model="fallback")]
        with self.assertRaises(BenchError): self.run_client()
        self.responses = ["redirect"]
        with self.assertRaises(BenchError): self.run_client()
        self.assertEqual(len(self.requests), 2)

    def test_missing_usage_never_gets_a_ranking(self):
        self.responses = [self.response("missing", usage=False)]
        result = self.run_client()
        self.assertIsNone(result["usage"]["ranking_total_tokens"])

    def test_endpoint_pin_changes_fail_before_request(self):
        self.endpoint["runtime"]["precision"] = "FP8"
        with self.assertRaises(BenchError): self.run_client()
        self.assertEqual(self.requests, [])

    def test_real_native_ledger_shape_and_reconciliation(self):
        totals = {"inputTokens": 100, "outputTokens": 20, "cachedReadTokens": 60, "reasoningTokens": 10, "totalTokens": 120, "modelCalls": 2, "modelUsage": {"fixture-model": {}}}
        value = normalize_grok([], {"session": totals, "turns": [{"inputTokens": 100, "outputTokens": 20}]})
        self.assertEqual(value["ranking_total_tokens"], 120)
        self.assertEqual(value["cached_input_tokens"], 60)
        self.assertEqual(value["model_calls"], 2)
        value = normalize_grok([], {"session": totals, "turns": [{"inputTokens": 10, "outputTokens": 20}]})
        self.assertIsNone(value["ranking_total_tokens"])


if __name__ == "__main__":
    unittest.main()
