import base64
from copy import deepcopy
from decimal import Decimal
import json
from pathlib import Path
import tempfile
import unittest

from kilix_agent_bench.core import BenchError, Store, atomic_json
from kilix_agent_bench.dataset import Dataset, finalize, verify_evidence
from kilix_agent_bench.scenarios import TASK_ROUTES, admit, case, score
from kilix_agent_bench.spending import RentalLedger
from kilix_agent_bench.telemetry import Timings, append_event, read_events, summarize_timings
from kilix_agent_bench.usage import aggregate, normalize_codex, normalize_grok, normalize_messages, response_usage


class ScorerTests(unittest.TestCase):
    def fixtures(self, task):
        spec = case(task, 220200)
        before = {"unrelated": "fixed", "protected_hashes": {"fixture": "same"}, "ram_bytes": 4096 * 1024,
                  "expected_matches": [{"path": "a λ.txt", "line": 2}], "active_log": "/fixture/active.jsonl", "cwd": "/fixture"}
        after = {"unrelated": "fixed", "protected_hashes": {"fixture": "same"}, "caller_preserved": True,
                 "new_panes": [], "input_bytes_base64": "", "workers": []}
        answers = {"files_literal_search": {"matches": [{"path": "a λ.txt", "line": 2}]},
                   "system_ram_total": {"total": 4096, "unit": "kB"},
                   "active_session_log_retrieval": {"path": "/fixture/active.jsonl", "text": spec["log_text"]},
                   "pane_visible_text_read": {"text": spec["screen_text"]},
                   "pane_literal_without_submit": {"done": True}, "single_worker_launch": {"done": True}}
        if task == "pane_literal_without_submit":
            after["input_bytes_base64"] = base64.b64encode(spec["input_text"].encode()).decode()
        if task == "single_worker_launch":
            after["new_panes"] = ["2"]
            after["workers"] = [{"model": spec["child_model"], "effort": "high", "cwd": "/fixture", "ready": True, "alive": True}]
        return spec, before, after, answers[task]

    def test_all_six_positive_controls_and_unrelated_mutation(self):
        for task in TASK_ROUTES:
            with self.subTest(task=task):
                spec, before, after, answer = self.fixtures(task)
                self.assertTrue(score(spec, before, after, json.dumps(answer), route_ok=True)["eligible_success"])
                after["unrelated"] = "changed"
                self.assertEqual(score(spec, before, after, json.dumps(answer), route_ok=True)["outcome"], "valid_fail")

    def test_success_does_not_imply_route_compliance(self):
        args = self.fixtures("system_ram_total")
        outcome = score(*args[:3], json.dumps(args[3]), route_ok=False)
        self.assertEqual(outcome["outcome"], "valid_pass")
        self.assertFalse(outcome["eligible_success"])

    def test_file_search_duplicates_wrong_line_and_scope_escape_fail(self):
        spec, before, after, answer = self.fixtures("files_literal_search")
        for matches in (answer["matches"] * 2, [{"path": "a λ.txt", "line": 1}], [{"path": "../a λ.txt", "line": 2}], []):
            self.assertFalse(score(spec, before, after, json.dumps({"matches": matches}), route_ok=True)["eligible_success"])

    def test_enter_and_literal_corruption_are_detected(self):
        spec, before, after, answer = self.fixtures("pane_literal_without_submit")
        for data in (spec["input_text"].encode() + b"\r", b"wrong", b""):
            after["input_bytes_base64"] = base64.b64encode(data).decode()
            self.assertFalse(score(spec, before, after, json.dumps(answer), route_ok=True)["eligible_success"])

    def test_wrong_units_and_nonfinite_memory_fail(self):
        spec, before, after, _ = self.fixtures("system_ram_total")
        for answer in ({"total": 4096, "unit": "bytes"}, {"total": 4096, "unit": "GB"}, {"total": True, "unit": "kB"}, {"total": float("nan"), "unit": "kB"}):
            self.assertFalse(score(spec, before, after, json.dumps(answer), route_ok=True)["eligible_success"])

    def test_stale_log_and_user_message_fail(self):
        spec, before, after, answer = self.fixtures("active_session_log_retrieval")
        for update in ({"path": "/fixture/stale.jsonl"}, {"text": "latest user text"}):
            self.assertFalse(score(spec, before, after, json.dumps({**answer, **update}), route_ok=True)["eligible_success"])

    def test_scrollback_or_injected_input_fails(self):
        spec, before, after, answer = self.fixtures("pane_visible_text_read")
        self.assertFalse(score(spec, before, after, '{"text":"SCROLLBACK_DECOY"}', route_ok=True)["eligible_success"])
        after["input_bytes_base64"] = "eA=="
        self.assertFalse(score(spec, before, after, json.dumps(answer), route_ok=True)["eligible_success"])

    def test_duplicate_wrong_model_dead_worker_and_no_effect_fail(self):
        spec, before, after, answer = self.fixtures("single_worker_launch")
        variants = []
        for field, value in (("model", "other"), ("effort", "low"), ("cwd", "/wrong"), ("alive", False), ("ready", False)):
            bad = deepcopy(after); bad["workers"][0][field] = value; variants.append(bad)
        variants += [{**after, "workers": []}, {**after, "workers": after["workers"] * 2}, {**after, "new_panes": ["2", "3"]}]
        for bad in variants:
            self.assertFalse(score(spec, before, bad, json.dumps(answer), route_ok=True)["eligible_success"])

    def test_route_contract_and_model_only_admission(self):
        self.assertEqual(sum(map(len, TASK_ROUTES.values())), 16)
        self.assertFalse(admit("native_shell", ["kilix", "grammar", "show memory"]))
        self.assertFalse(admit("ask_lfm2_model_only_cli", ["kilix-needle", "ask", "--commands-backend", "lfm2", "open worker"]))
        self.assertTrue(admit("ask_lfm2_model_only_cli", ["kilix-needle", "ask", "--commands-backend", "lfm2", "--commands-model-only", "open worker"]))
        self.assertFalse(admit("kilix_system_cli", ["sh", "-c", "kilix-system --json"]))


class UsageTests(unittest.TestCase):
    def test_messages_cache_components_are_inclusive_exactly_once(self):
        row = response_usage("r", {"input_tokens": 10, "cache_read_input_tokens": 80, "cache_creation_input_tokens": 20, "output_tokens": 7}, "messages")
        self.assertEqual((row["input_tokens"], row["total_tokens"]), (110, 117))

    def test_reasoning_and_cache_subsets_not_added_again(self):
        row = response_usage("r", {"prompt_tokens": 100, "completion_tokens": 20, "prompt_tokens_details": {"cached_tokens": 80}, "completion_tokens_details": {"reasoning_tokens": 15}}, "chat")
        self.assertEqual(row["total_tokens"], 120)
        self.assertEqual(aggregate([row, row])["total_tokens"], 120)

    def test_missing_usage_and_zero_responses_are_unknown(self):
        self.assertIsNone(aggregate([])["ranking_total_tokens"])
        self.assertIsNone(aggregate([response_usage("r", {}, "chat")])["ranking_total_tokens"])

    def test_conflicting_response_id_is_rejected(self):
        first = response_usage("r", {"input_tokens": 1, "output_tokens": 2}, "responses")
        second = response_usage("r", {"input_tokens": 1, "output_tokens": 3}, "responses")
        with self.assertRaises(BenchError): aggregate([first, second])

    def test_cumulative_snapshots_deduplicate_and_reject_decreases(self):
        def event(i, o):
            return {"type": "event_msg", "payload": {"type": "token_count", "info": {"total_token_usage": {"input_tokens": i, "output_tokens": o, "cached_input_tokens": 0}}}}
        value = normalize_codex([], [event(100, 5), event(100, 5), event(250, 9)])
        self.assertEqual((value["response_count"], value["total_tokens"]), (2, 259))
        with self.assertRaises(BenchError): normalize_codex([], [event(100, 5), event(1, 1)])

    def test_native_message_blocks_reconcile_with_session_totals(self):
        usage = {"input_tokens": 5, "cache_read_input_tokens": 20, "cache_creation_input_tokens": 30, "output_tokens": 3}
        events = [{"type": "assistant", "message": {"id": "a", "model": "test-model", "usage": usage}},
                  {"type": "assistant", "message": {"id": "a", "model": "test-model", "usage": {**usage, "output_tokens": 5}}},
                  {"type": "result", "usage": {**usage, "output_tokens": 5}}]
        value = normalize_messages(events)
        self.assertEqual((value["response_count"], value["total_tokens"]), (1, 60))
        events[-1]["usage"]["output_tokens"] = 6
        self.assertIsNone(normalize_messages(events)["ranking_total_tokens"])

    def test_session_ledger_aliases(self):
        value = normalize_grok([], {"totals": {"inputTokens": 100, "outputTokens": 8, "cachedInputTokens": 50}})
        self.assertEqual(value["total_tokens"], 108)


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "store")

    def tearDown(self): self.tmp.cleanup()

    def attempt(self, ident="attempt-a", kind="scored", success=True, tokens=10):
        path = self.store.root / "runs" / ident; path.mkdir(mode=0o700)
        usage = aggregate([response_usage("r", {"input_tokens": tokens, "output_tokens": 0}, "responses")])
        atomic_json(path / "result.json", {"schema": "kilix-agent-bench/attempt-v1", "run_kind": kind,
                    "attempt_id": ident, "logical_trial_id": ident, "track": "hosted_api", "configuration_id": "test-low",
                    "protocol_sha256": "p", "manifest_sha256": "m", "task": "system_ram_total", "route": "native_shell",
                    "seed": 1, "repetition": 0, "cleanup": "passed", "usage": usage,
                    "outcome": {"outcome": "valid_pass" if success else "valid_fail", "eligible_success": success}})
        finalize(path)
        return path

    def test_dataset_reimport_is_idempotent_and_failures_count(self):
        dataset = Dataset(self.store)
        first = self.attempt(tokens=10)
        self.assertTrue(dataset.ingest(first)["added"])
        self.assertFalse(dataset.ingest(first)["added"])
        dataset.ingest(self.attempt("attempt-b", success=False, tokens=30))
        summary = dataset.report()["groups"][0]
        self.assertEqual(summary["controller_tokens_per_verified_success"], 40)

    def test_qualification_never_enters_scored_dataset(self):
        with self.assertRaises(BenchError): Dataset(self.store).ingest(self.attempt(kind="qualification"))

    def test_mutated_evidence_and_added_files_are_detected(self):
        path = self.attempt()
        (path / "extra.txt").write_text("changed")
        with self.assertRaises(BenchError): verify_evidence(path)

    def test_zero_success_has_no_savings_score(self):
        dataset = Dataset(self.store); dataset.ingest(self.attempt(success=False))
        self.assertIsNone(dataset.report()["groups"][0]["controller_tokens_per_verified_success"])

    def test_timing_interruption_and_missing_end_are_retained(self):
        p = self.store.root / "reports/timing.jsonl"
        with self.assertRaises(RuntimeError):
            with Timings(p).span("setup"):
                raise RuntimeError("interrupted")
        append_event(p, {"event": "start", "span_id": "unfinished", "phase": "trial"})
        result = summarize_timings(read_events(p))
        self.assertEqual(result["unfinished_span_ids"], ["unfinished"])
        self.assertEqual(result["groups"][0]["n"], 1)
        self.assertIsNone(result["groups"][0]["p90_s"])

    def receipt(self, kind, ident, at, **extra):
        return {"event_id": ident, "kind": kind, "at": at, "source": "fixture-receipt", "source_sha256": "0" * 64, **extra}

    def test_spending_keeps_quotes_estimates_charges_and_credits_separate(self):
        ledger = RentalLedger(self.store)
        ledger.record(self.receipt("quote", "q", "2026-01-01T00:00:00Z", quote={"rate": "2"}))
        self.assertEqual(ledger.report()["recorded_net_usd"], "0")
        ledger.record(self.receipt("instance_created", "created", "2026-01-01T00:00:00Z", instance_id="123", rates={"compute_per_hour": "2", "storage_per_hour": "0.1"}))
        ledger.record(self.receipt("instance_state", "run", "2026-01-01T00:00:00Z", instance_id="123", state="running"))
        ledger.record(self.receipt("instance_state", "stop", "2026-01-01T01:00:00Z", instance_id="123", state="stopped"))
        ledger.record(self.receipt("instance_state", "destroy", "2026-01-01T03:00:00Z", instance_id="123", state="destroyed"))
        charge = self.receipt("charge", "charge", "2026-01-01T04:00:00Z", instance_id="123", usd="2.5", billing_id="line-1")
        ledger.record(charge); ledger.record(charge)
        ledger.record(self.receipt("credit", "credit", "2026-01-01T04:00:00Z", instance_id="123", usd="0.25", billing_id="credit-1"))
        summary = ledger.report(at="2026-01-01T05:00:00Z")
        self.assertEqual(Decimal(summary["estimated_compute_storage_usd"]), Decimal("2.3"))
        self.assertEqual(summary["recorded_net_usd"], "2.25")
        self.assertEqual(summary["instances"][0]["state"], "destroyed")
        with self.assertRaises(BenchError): ledger.record({**charge, "event_id": "duplicate-line"})

    def test_other_project_instance_and_invalid_amount_are_rejected(self):
        ledger = RentalLedger(self.store)
        with self.assertRaises(BenchError): ledger.record(self.receipt("charge", "c", "2026-01-01T00:00:00Z", instance_id="999", usd="1", billing_id="line"))
        with self.assertRaises(BenchError): ledger.record(self.receipt("instance_created", "bad", "2026-01-01T00:00:00Z", instance_id="123", rates={"compute_per_hour": "NaN", "storage_per_hour": "0"}))


if __name__ == "__main__": unittest.main()
