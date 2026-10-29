import hashlib
import io
import json
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import MagicMock

from kilix_agent_bench.core import BenchError, Store
from kilix_agent_bench.stack import capture, git_snapshot, load_lock, provision, relative, validate_bundle


class StackTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def bundle(self, names=("payload/file",), *, content=b"hello", actual=None, link=False):
        files = [{"path": n, "sha256": hashlib.sha256(content).hexdigest(), "bytes": len(content), "mode": 0o644}
                 for n in names]
        manifest = {"schema": "kilix-agent-bench/stack-payload-v1", "benchmark_version": "0.2.2",
                    "config": {"schema": "kilix-agent-bench/config-v1",
                               "image": {"path": "/base.qcow2", "sha512": "a" * 128},
                               "vm": {"cpus": 1, "memory_mib": 512, "disk_gib": 4, "boot_timeout_s": 60}},
                    "sources": [], "packages": [], "setup": [], "files": files, "directories": ["payload", "packages"]}
        path = self.root / "stack.tar"
        with tarfile.open(path, "w") as archive:
            data = json.dumps(manifest).encode()
            info = tarfile.TarInfo("manifest.json")
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
            for name in names:
                info = tarfile.TarInfo(name)
                info.mode = 0o644
                data = content if actual is None else actual
                info.size = len(data)
                if link:
                    info.type, info.linkname, info.size = tarfile.SYMTYPE, "/etc/passwd", 0
                    archive.addfile(info)
                else:
                    archive.addfile(info, io.BytesIO(data))
        return path

    def test_member_substitution_is_rejected(self):
        self.assertEqual(len(validate_bundle(self.bundle())["files"]), 1)
        with self.assertRaisesRegex(BenchError, "digest"):
            validate_bundle(self.bundle(actual=b"other"))

    def test_traversal_and_ambiguous_paths_are_rejected(self):
        for name in ("../file", "/file", "payload/../file", "payload//file", "payload/./file", "payload\\file"):
            with self.subTest(name=name), self.assertRaises(BenchError):
                validate_bundle(self.bundle(names=(name,)))

    def test_links_duplicates_and_parent_file_conflicts_are_rejected(self):
        for names, link in ((("payload/file",), True), (("payload/file", "payload/file"), False),
                            (("payload/file", "payload/file/child"), False)):
            with self.subTest(names=names, link=link), self.assertRaises(BenchError):
                validate_bundle(self.bundle(names=names, link=link))

    def test_truncated_member_set_is_rejected(self):
        path = self.bundle()
        with tarfile.open(path) as archive:
            manifest = archive.extractfile("manifest.json").read()
        with tarfile.open(path, "w") as archive:
            info = tarfile.TarInfo("manifest.json")
            info.size = len(manifest)
            archive.addfile(info, io.BytesIO(manifest))
        with self.assertRaisesRegex(BenchError, "missing"):
            validate_bundle(path)

    def test_git_snapshot_uses_commit_objects_without_local_config_or_changes(self):
        source, target = self.root / "source", self.root / "snapshot"
        source.mkdir()
        def git(*argv):
            return subprocess.check_output(["git", "-C", str(source), *argv], stderr=subprocess.DEVNULL).decode().strip()
        git("init", "--quiet", "--template=")
        (source / "file").write_text("committed")
        git("add", "file")
        git("-c", "user.name=itsmygithubacct", "-c", "user.email=itsmygithubacct@users.noreply.github.com",
            "commit", "--quiet", "-m", "Fixture source")
        commit = git("rev-parse", "HEAD")
        git("config", "remote.origin.url", "https://private-user:synthetic-password@example.org/source")
        git("config", "credential.helper", "fixture-helper")
        (source / "file").write_text("dirty")
        (source / "private-untracked").write_text("do not copy")
        records = []
        git_snapshot(source, commit, target, records, "sources/component", "https://example.org/source.git")
        self.assertEqual((target / "file").read_text(), "committed")
        self.assertFalse((target / "private-untracked").exists())
        config = (target / ".git/config").read_text()
        self.assertNotIn("synthetic-password", config)
        self.assertNotIn("fixture-helper", config)
        self.assertFalse((target / ".git/objects/info/alternates").exists())
        self.assertFalse((target / ".git/FETCH_HEAD").exists())
        self.assertEqual(records[0]["commit"], commit)
        self.assertEqual((source / "file").read_text(), "dirty")
        inputs = self.root / "git-inputs.json"
        template = json.loads(self.recipe().read_text())
        template["trees"] = []
        template["repositories"] = [{"path": str(source), "commit": commit,
                                     "repository": "https://example.org/source.git", "destination": "sources/component"}]
        inputs.write_text(json.dumps(template))
        frozen = capture(Store(self.root / "git-data"), inputs)
        _, _, manifest, _ = load_lock(frozen["lock"])
        self.assertIn("payload/sources/component/.git/refs/heads", manifest["directories"])

    def recipe(self):
        data = self.root / "input"
        data.mkdir()
        (data / "hello").write_bytes(b"known bytes")
        value = {"schema": "kilix-agent-bench/stack-inputs-v1", "benchmark_version": "0.2.2",
                 "config": {"schema": "kilix-agent-bench/config-v1",
                            "image": {"path": "base.qcow2", "sha512": "a" * 128},
                            "vm": {"cpus": 1, "memory_mib": 512, "disk_gib": 4, "boot_timeout_s": 60}},
                 "repositories": [], "trees": [{"path": "input", "destination": "component"}],
                 "files": [], "packages": [], "setup": []}
        path = self.root / "inputs.json"
        path.write_text(json.dumps(value))
        return path

    def test_captured_bundle_is_independent_and_tampering_blocks_guest_mutation(self):
        store = Store(self.root / "data")
        result = capture(store, self.recipe())
        lock, bundle, manifest, config = load_lock(result["lock"])
        self.assertEqual(manifest["benchmark_version"], "0.2.2")
        (self.root / "input/hello").write_text("changed later")
        self.assertEqual(load_lock(result["lock"])[0], lock)
        bundle.chmod(0o600)
        bundle.write_bytes(b"changed bundle")
        vm = MagicMock()
        vm.state = {"config": config}
        with self.assertRaisesRegex(BenchError, "digest"):
            provision(vm, result["lock"])
        vm.upload.assert_not_called()
        vm.ssh.assert_not_called()
        vm.save.assert_not_called()

    def test_stack_for_another_base_cannot_be_provisioned(self):
        result = capture(Store(self.root / "data"), self.recipe())
        vm = MagicMock()
        vm.state = {"config": {}}
        with self.assertRaisesRegex(BenchError, "configuration differs"):
            provision(vm, result["lock"])
        vm.upload.assert_not_called()

    def test_changed_vm_configuration_cannot_reuse_a_frozen_identity(self):
        store = Store(self.root / "data")
        path = self.recipe()
        first = capture(store, path)
        initial = Path(first["lock"]).read_bytes()
        same = capture(store, path)
        self.assertEqual(first["sha256"], same["sha256"])
        self.assertEqual(Path(same["lock"]).read_bytes(), initial)
        value = json.loads(path.read_text())
        value["config"]["vm"]["memory_mib"] = 1024
        path.write_text(json.dumps(value))
        second = capture(store, path)
        self.assertNotEqual(first["sha256"], second["sha256"])
        self.assertEqual(Path(first["lock"]).read_bytes(), initial)
        value = json.loads(initial)
        value["config"]["vm"]["memory_mib"] = 1024
        Path(first["lock"]).write_text(json.dumps(value))
        with self.assertRaisesRegex(BenchError, "configuration mismatch"):
            load_lock(first["lock"])

    def test_overlapping_destinations_are_rejected(self):
        path = self.recipe()
        value = json.loads(path.read_text())
        value["trees"].append({"path": "input", "destination": "component/child"})
        path.write_text(json.dumps(value))
        with self.assertRaisesRegex(BenchError, "overlap"):
            capture(Store(self.root / "data"), path)

    def test_tree_cannot_copy_an_external_symlink(self):
        path = self.recipe()
        (self.root / "outside").write_text("private")
        (self.root / "input/link").symlink_to(self.root / "outside")
        with self.assertRaisesRegex(BenchError, "contained"):
            capture(Store(self.root / "data"), path)
