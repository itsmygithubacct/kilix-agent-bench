import base64
import fcntl
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from kilix_agent_bench.core import (BenchError, Store, acquire_image, atomic_json,
                                   command, digest, validate_config)
from kilix_agent_bench.tasks import OBSERVE, payload, verify
from kilix_agent_bench.qualification import qualify
from kilix_agent_bench.vm import VM, process_info


def config():
    return {"schema": "kilix-agent-bench/config-v1",
            "image": {"path": "base.qcow2", "sha512": "a" * 128},
            "vm": {"cpus": 1, "memory_mib": 512, "disk_gib": 4, "boot_timeout_s": 60}}


class ConfigTests(unittest.TestCase):
    def test_unknown_fields_fail(self):
        for where in (None, "image", "vm"):
            value = config()
            (value if where is None else value[where])["typo"] = True
            with self.subTest(where=where), self.assertRaises(BenchError):
                validate_config(value)

    def test_missing_and_boolean_resources_fail(self):
        for key in config()["vm"]:
            value = config()
            value["vm"][key] = True
            with self.subTest(key=key), self.assertRaises(BenchError):
                validate_config(value)
        value = config()
        del value["vm"]["cpus"]
        with self.assertRaises(BenchError):
            validate_config(value)

    def test_unsafe_urls_and_missing_pin_fail(self):
        for url in ("http://example.org/a", "https://name:password@example.org/a", "https://example.org/a?secret=value"):
            value = config()
            value["image"] = {"url": url, "sha512": "a" * 128}
            with self.subTest(url=url), self.assertRaises(BenchError):
                validate_config(value)
        value["image"] = {"url": "https://example.org/a"}
        with self.assertRaises(BenchError):
            validate_config(value)

    def test_local_input_is_resolved_relative_to_manifest(self):
        value = config()
        base = Path(tempfile.gettempdir()) / "manifest"
        actual = validate_config(value, base)
        self.assertEqual(actual["image"]["path"], str(base / "base.qcow2"))
        self.assertEqual(value["image"]["path"], "base.qcow2")


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "runtime")

    def test_runtime_is_private_and_vm_ids_cannot_escape(self):
        self.assertEqual(self.store.root.stat().st_mode & 0o777, 0o700)
        for ident in ("../../target", "vm-ABC", "vm-123456789abc/../../target"):
            with self.subTest(ident=ident), self.assertRaises(BenchError):
                self.store.vm_dir(ident)
        (self.store.root / "vms" / "vm-123456789abc").symlink_to(self.root)
        with self.assertRaises(BenchError):
            self.store.vm_dir("vm-123456789abc")

    def test_existing_open_directory_is_rejected_without_chmod(self):
        path = self.root / "public"
        path.mkdir(mode=0o755)
        path.chmod(0o755)
        with self.assertRaises(BenchError):
            Store(path)
        self.assertEqual(path.stat().st_mode & 0o777, 0o755)

    def test_atomic_result_is_private_and_replaces_valid_json(self):
        p = self.store.root / "runs" / "result.json"
        atomic_json(p, {"status": "running"})
        atomic_json(p, {"status": "passed"})
        self.assertEqual(json.loads(p.read_text()), {"status": "passed"})
        self.assertEqual(p.stat().st_mode & 0o777, 0o600)

    def test_wrong_image_digest_never_enters_cache(self):
        source = self.root / "source.qcow2"
        source.write_bytes(b"invalid input")
        value = config()
        value["image"]["path"] = str(source)
        with self.assertRaisesRegex(BenchError, "checksum mismatch"):
            acquire_image(self.store, value)
        self.assertEqual(list((self.store.root / "images").iterdir()), [])
        self.assertEqual(list((self.store.root / "cache").iterdir()), [])

    @unittest.skipUnless(shutil.which("qemu-img"), "qemu-img is required")
    def test_standalone_image_cache_and_backing_file_rejection(self):
        source = self.root / "base.qcow2"
        command(["qemu-img", "create", "-f", "qcow2", str(source), "4G"])
        value = config()
        value["image"] = {"path": str(source), "sha512": digest(source, "sha512")}
        cached = acquire_image(self.store, value)
        self.assertEqual(digest(cached, "sha512"), value["image"]["sha512"])
        overlay = self.root / "overlay.qcow2"
        command(["qemu-img", "create", "-f", "qcow2", "-F", "qcow2", "-b", str(source), str(overlay)])
        value["image"] = {"path": str(overlay), "sha512": digest(overlay, "sha512")}
        with self.assertRaisesRegex(BenchError, "standalone"):
            acquire_image(self.store, value)
        self.assertEqual(len(list((self.store.root / "images").iterdir())), 1)

    def fake_vm(self, pid=None):
        ident = "vm-123456789abc"
        p = self.store.vm_dir(ident)
        p.mkdir(mode=0o700)
        atomic_json(p / "state.json", {"schema": "kilix-agent-bench/vm-v1", "id": ident,
                    "config": config(), "phase": "stopped", "generation": 0,
                    "uuid": "fixture-uuid", "process": process_info(pid) if pid else None,
                    "qemu_exe": "/not-a-qemu-binary"})
        return VM(self.store, ident)

    def test_pid_reuse_or_foreign_process_is_not_signaled_or_deleted(self):
        process = subprocess.Popen(["sleep", "30"])
        try:
            vm = self.fake_vm(process.pid)
            with self.assertRaisesRegex(BenchError, "ownership mismatch"):
                vm.destroy()
            self.assertIsNone(process.poll())
            self.assertTrue(vm.path.exists())
        finally:
            process.terminate()
            process.wait(timeout=5)

    def test_unreadable_process_is_not_assumed_dead(self):
        process = subprocess.Popen(["sleep", "30"])
        try:
            vm = self.fake_vm(process.pid)
            original = Path.read_text
            def read(path, *args, **kwargs):
                if path == Path('/proc') / str(process.pid) / 'stat':
                    raise PermissionError('process inspection denied')
                return original(path, *args, **kwargs)
            with patch.object(Path, 'read_text', read):
                with self.assertRaisesRegex(BenchError, 'inspect'):
                    vm.destroy()
            self.assertTrue(vm.path.exists())
            self.assertIsNone(process.poll())
        finally:
            process.terminate()
            process.wait(timeout=5)

    def test_competing_lifecycle_operation_is_refused(self):
        vm = self.fake_vm()
        with (self.store.root / "vms" / f".{vm.ident}.lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            with self.assertRaisesRegex(BenchError, "Another operation"):
                vm.stop()

    def test_second_instance_reloads_state_before_operation(self):
        vm = self.fake_vm()
        other = VM(self.store, vm.ident)
        vm.state["generation"] = 4
        vm.save()
        self.assertEqual(other.status()["generation"], 4)

    def test_daemon_cwd_is_irrelevant_but_open_disk_and_uuid_are_required(self):
        vm = self.fake_vm()
        (vm.path / 'qemu.pid').write_text('12345')
        current = {'pid': 12345, 'exe': vm.state['qemu_exe'], 'cwd': '/',
                   'argv': ['qemu', '-name', vm.ident, '-uuid', vm.state['uuid']],
                   'open_files': [str(vm.path / 'disk.qcow2')]}
        with patch('kilix_agent_bench.vm.process_info', return_value=current):
            self.assertEqual(vm.owned_process(), current)
            current['open_files'] = []
            with self.assertRaisesRegex(BenchError, 'ownership mismatch'):
                vm.owned_process()

    def test_bound_process_can_close_disk_during_shutdown(self):
        vm = self.fake_vm()
        current = {'pid': 12345, 'exe': vm.state['qemu_exe'], 'cwd': '/',
                   'argv': ['qemu', '-name', vm.ident, '-uuid', vm.state['uuid']],
                   'open_files': [], 'start_ticks': '42', 'boot_id': 'boot-fixture'}
        vm.state['process'] = current.copy()
        with patch('kilix_agent_bench.vm.process_info', return_value=current):
            self.assertEqual(vm.owned_process(), current)
            current['start_ticks'] = '43'
            with self.assertRaisesRegex(BenchError, 'ownership mismatch'):
                vm.owned_process()

    def test_log_copy_failure_still_attempts_guest_teardown(self):
        vm = Mock()
        vm.ident = 'vm-123456789abc'
        vm.path = self.store.vm_dir(vm.ident)
        vm.path.mkdir(mode=0o700)
        (vm.path / 'serial.log').write_text('synthetic boot output')
        vm.state = {}
        vm.start.side_effect = BenchError('synthetic boot failure')
        vm.destroy.side_effect = lambda: shutil.rmtree(vm.path)
        with patch('kilix_agent_bench.qualification.VM.create', return_value=vm), \
             patch('kilix_agent_bench.qualification.shutil.copyfile', side_effect=OSError('disk full')):
            result = qualify(self.store, config())
        vm.destroy.assert_called_once()
        self.assertEqual(result['status'], 'invalid_infrastructure')
        self.assertEqual(result['cleanup'], 'passed')

    def test_progress_callback_failure_still_attempts_guest_teardown(self):
        vm = Mock()
        vm.ident = 'vm-123456789abc'
        vm.path = self.store.vm_dir(vm.ident)
        vm.path.mkdir(mode=0o700)
        vm.state = {}
        vm.start.side_effect = BenchError('synthetic boot failure')
        vm.destroy.side_effect = lambda: shutil.rmtree(vm.path)
        def progress(message):
            if message.startswith('Stopping'):
                raise OSError('progress output closed')
        with patch('kilix_agent_bench.qualification.VM.create', return_value=vm):
            result = qualify(self.store, config(), progress)
        vm.destroy.assert_called_once()
        self.assertEqual(result['cleanup'], 'passed')


def file_record(data):
    return {"kind": "file", "base64": base64.b64encode(data).decode()}


class OracleTests(unittest.TestCase):
    def setUp(self):
        self.content = payload()
        self.observed = {"result": file_record(self.content), "input": file_record(self.content),
                         "unrelated": file_record(b"preserve this file"),
                         "control": file_record(b"observer-owned control"),
                         "names": ["payload.txt", "result ; literal.txt", "unrelated.txt"]}

    def test_success_is_exact_bytes_with_no_added_newline(self):
        self.assertEqual(verify(self.observed, self.content, 0)["outcome"], "valid_pass")
        self.observed["result"] = file_record(self.content + b"\n")
        score = verify(self.observed, self.content, 0)
        self.assertEqual(score["outcome"], "valid_fail")
        self.assertEqual([k for k,v in score["checks"].items() if not v], ["exact_bytes"])

    def test_missing_symlink_or_special_file_cannot_pass(self):
        for kind in ("missing", "unreadable", "invalid-file", "oversized"):
            self.observed["result"] = {"kind": kind}
            self.assertEqual(verify(self.observed, self.content, 0)["outcome"], "valid_fail")

    def test_failed_actor_or_unrelated_mutation_cannot_pass(self):
        self.assertEqual(verify(self.observed, self.content, 1)["outcome"], "valid_fail")
        self.observed["names"].append("forbidden")
        self.assertEqual(verify(self.observed, self.content, 0)["outcome"], "valid_fail")
        self.observed["input"] = file_record(b"changed")
        self.assertFalse(verify(self.observed, self.content, 0)["checks"]["input_preserved"])

    def test_missing_observer_fields_are_infrastructure_errors(self):
        del self.observed["result"]
        with self.assertRaises(BenchError):
            verify(self.observed, self.content, 0)

    def test_corrupted_observer_control_is_not_a_task_failure(self):
        self.observed["control"] = file_record(b"changed")
        with self.assertRaisesRegex(BenchError, "integrity"):
            verify(self.observed, self.content, 0)

    def test_observer_read_does_not_follow_symlink_or_block_on_fifo(self):
        # Exercise the observer's actual read function on host-owned test files.
        namespace = {}
        exec(OBSERVE.split("print(json.dumps(", 1)[0], namespace)
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)
            (p / "secret").write_bytes(b"do not follow")
            (p / "link").symlink_to(p / "secret")
            os.mkfifo(p / "fifo")
            self.assertEqual(namespace["read"](p / "link")["kind"], "unreadable")
            self.assertEqual(namespace["read"](p / "fifo")["kind"], "invalid-file")


if __name__ == "__main__":
    unittest.main()
