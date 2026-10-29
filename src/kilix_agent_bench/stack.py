"""Freeze explicit local inputs and provision them offline in an owned VM.

An operator-selected stack is trusted software. Capture never imports a home
directory implicitly and never downloads or accepts a model license. Sources
are copied from exact Git objects into independent shallow repositories.
"""
from __future__ import annotations

import hashlib
import base64
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import shutil
import stat
import subprocess
import tarfile
import tempfile
import urllib.parse

from .core import BenchError, _keys, atomic_json, command, digest, now, private_dir, validate_config


MAX_BYTES = 16 * 1024**3
MAX_FILES = 100000


def relative(value):
    if (not isinstance(value, str) or not value or "\0" in value or "\\" in value
            or value.startswith("/") or any(p in ("", ".", "..") for p in value.split("/"))):
        raise BenchError("Stack paths must be normalized relative POSIX paths")
    return value


def _sha(value, length=64):
    if not isinstance(value, str) or not re.fullmatch(f"[0-9a-f]{{{length}}}", value):
        raise BenchError(f"Expected a lowercase {length}-digit digest")
    return value


def _repository(value):
    if not isinstance(value, str):
        raise BenchError("Source identity must be an HTTPS URL")
    url = urllib.parse.urlsplit(value)
    if url.scheme != "https" or not url.hostname or url.username or url.password or url.query or url.fragment:
        raise BenchError("Source identity must be an HTTPS URL without credentials")
    return value


def _steps(value):
    if not isinstance(value, list):
        raise BenchError("Stack setup must be a list")
    for step in value:
        _keys(step, {"id", "argv", "timeout_s"}, {"id", "argv", "timeout_s"}, "setup step")
        if not isinstance(step["id"], str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", step["id"]):
            raise BenchError("Setup IDs must be short lowercase identifiers")
        if (not isinstance(step["argv"], list) or not step["argv"]
                or not all(isinstance(a, str) and "\0" not in a for a in step["argv"])):
            raise BenchError("Setup argv must be a nonempty list of strings")
        if type(step["timeout_s"]) is not int or not 1 <= step["timeout_s"] <= 1800:
            raise BenchError("Setup timeout must be an integer in 1..1800")
    if len({s["id"] for s in value}) != len(value):
        raise BenchError("Setup IDs must be unique")
    return value


def git_snapshot(source, commit, target, records, destination, repository):
    """Keep only selected history, with no inherited remotes/config/credentials."""
    source = Path(source).resolve()
    _repository(repository)
    _sha(commit, 40)
    actual = command(["git", "-C", str(source), "rev-parse", commit + "^{commit}"]).decode().strip()
    if actual != commit:
        raise BenchError("Source commit did not resolve exactly")
    target.mkdir(parents=True, exist_ok=True)
    command(["git", "init", "--quiet", "--template=", str(target)])
    command(["git", "-C", str(target), "-c", "protocol.file.allow=always", "fetch",
             "--quiet", "--no-write-fetch-head", "--depth=1", "--no-tags", source.as_uri(), commit], timeout=180)
    command(["git", "-C", str(target), "-c", "submodule.recurse=false", "-c", "core.hooksPath=/dev/null",
             "checkout", "--quiet", "--detach", commit])
    command(["git", "-C", str(target), "remote", "add", "origin", repository])
    records.append({"destination": destination, "commit": commit, "repository": repository})
    module_urls = {}
    if (target / ".gitmodules").exists():
        paths = command(["git", "config", "--file", str(target / ".gitmodules"), "--get-regexp", r"^submodule\..*\.path$"]).decode()
        for row in paths.splitlines():
            key, path = row.split(None, 1)
            module_urls[path] = command(["git", "config", "--file", str(target / ".gitmodules"),
                                         "--get", key[:-5] + ".url"]).decode().strip()
    tree = command(["git", "-C", str(target), "ls-tree", "-r", "-z", commit])
    for line in tree.split(b"\0"):
        if not line:
            continue
        meta, raw = line.split(b"\t", 1)
        mode, kind, obj = meta.decode().split()
        if mode == "160000":
            name = relative(os.fsdecode(raw))
            if name not in module_urls:
                raise BenchError("A gitlink has no declared source URL")
            url = urllib.parse.urljoin(repository + "/", module_urls[name])
            git_snapshot(source / name, obj, target / name, records, destination + "/" + name, url)
    if (target / ".gitmodules").exists():
        command(["git", "-C", str(target), "submodule", "init"])
    # Reflogs/FETCH_HEAD can contain acquisition paths. Neither is needed by
    # immutable source verification; no remote was configured by fetch.
    if (target / ".git/logs").exists():
        shutil.rmtree(target / ".git/logs")
    (target / ".git/FETCH_HEAD").unlink(missing_ok=True)


def _copy_tree(source, target):
    source = source.resolve(strict=True)
    if not source.is_dir():
        raise BenchError("Tree input must be a directory")
    target.mkdir(parents=True, exist_ok=False)
    for root, dirs, files in os.walk(source, followlinks=False):
        base = Path(root)
        dirs[:] = sorted(d for d in dirs if d not in ("__pycache__",))
        for name in dirs:
            path = base / name
            if path.is_symlink():
                raise BenchError(f"Directory symlink is not a supported tree input: {path}")
            (target / path.relative_to(source)).mkdir(exist_ok=True)
        for name in sorted(files):
            path = base / name
            resolved = path.resolve(strict=True)
            if not resolved.is_relative_to(source) or not resolved.is_file():
                raise BenchError(f"Tree member is not a contained regular file: {path}")
            shutil.copy2(resolved, target / path.relative_to(source))


def capture(store, recipe_path, progress=lambda message: None):
    """Create a self-contained artifact and lock; no guests or model calls."""
    recipe_path = Path(recipe_path).resolve()
    value = json.loads(recipe_path.read_text())
    fields = {"schema", "benchmark_version", "config", "repositories", "trees", "files", "packages", "setup"}
    _keys(value, fields, fields, "stack inputs")
    if value["schema"] != "kilix-agent-bench/stack-inputs-v1":
        raise BenchError("Unsupported stack input schema")
    if not isinstance(value["benchmark_version"], str) or not re.fullmatch(r"\d+\.\d+\.\d+", value["benchmark_version"]):
        raise BenchError("A benchmark release version is required")
    config = validate_config(value["config"], recipe_path.parent)
    setup = _steps(value["setup"])
    for name in ("repositories", "trees", "files", "packages"):
        if not isinstance(value[name], list):
            raise BenchError(f"{name} must be a list")
    local = lambda p: (recipe_path.parent / Path(p).expanduser()).resolve(strict=True)
    with tempfile.TemporaryDirectory(prefix="stack-", dir=store.root / "cache") as raw:
        stage = Path(raw)
        payload = stage / "payload"
        payload.mkdir()
        sources = []
        destinations = []
        for group in ("repositories", "trees", "files"):
            for item in value[group]:
                required = {"path", "destination"} | ({"commit", "repository"} if group == "repositories" else set())
                _keys(item, required, required, group)
                destination = relative(item["destination"])
                if any(destination == old or destination.startswith(old + "/") or old.startswith(destination + "/") for old in destinations):
                    raise BenchError("Stack destinations must not overlap")
                destinations.append(destination)
                progress(f"Capturing {destination}")
                target = payload / destination
                target.parent.mkdir(parents=True, exist_ok=True)
                if group == "repositories":
                    git_snapshot(local(item["path"]), item["commit"], target, sources, destination, item["repository"])
                elif group == "trees":
                    _copy_tree(local(item["path"]), target)
                else:
                    if not local(item["path"]).is_file():
                        raise BenchError("Explicit file input must be a regular file")
                    shutil.copy2(local(item["path"]), target)
        package_records = []
        (stage / "packages").mkdir()
        for item in value["packages"]:
            _keys(item, {"path", "sha256"}, {"path", "sha256"}, "package")
            sha = _sha(item["sha256"])
            source = local(item["path"])
            if digest(source) != sha:
                raise BenchError("Package digest mismatch")
            target = stage / "packages" / (sha + ".deb")
            if target.exists():
                raise BenchError("Duplicate package input")
            shutil.copyfile(source, target)
            identity = command(["dpkg-deb", "-f", str(target), "Package", "Version", "Architecture"]).decode().strip()
            package_records.append({"path": "packages/" + target.name, "sha256": sha, "identity": identity})
        files, directories = [], ["payload", "packages"]
        total = 0
        for prefix in ("payload", "packages"):
            for path in sorted((stage / prefix).rglob("*")):
                if path.is_symlink():
                    raise BenchError(f"Snapshot symlinks are not supported: {path.relative_to(stage)}")
                if path.is_dir():
                    directories.append(relative(path.relative_to(stage).as_posix()))
                    continue
                if not stat.S_ISREG(path.stat().st_mode):
                    raise BenchError("Only regular files may enter a stack bundle")
                size = path.stat().st_size
                total += size
                files.append({"path": relative(path.relative_to(stage).as_posix()), "bytes": size,
                              "sha256": digest(path), "mode": 0o755 if path.stat().st_mode & 0o111 else 0o644})
                if total > MAX_BYTES or len(files) > MAX_FILES:
                    raise BenchError("Stack exceeds the 16 GiB / 100000-file bound")
        manifest = {"schema": "kilix-agent-bench/stack-payload-v1", "benchmark_version": value["benchmark_version"],
                    "config": config, "sources": sources, "packages": package_records, "setup": setup,
                    "files": files, "directories": sorted(directories)}
        bundle = stage / "stack.tar"
        progress(f"Freezing {len(files)} files ({total} bytes)")
        with tarfile.open(bundle, "w") as archive:
            encoded = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
            info = tarfile.TarInfo("manifest.json")
            info.size, info.mode = len(encoded), 0o600
            archive.addfile(info, io.BytesIO(encoded))
            for entry in files:
                info = tarfile.TarInfo(entry["path"])
                info.size, info.mode = entry["bytes"], entry["mode"]
                with (stage / entry["path"]).open("rb") as stream:
                    archive.addfile(info, stream)
        validate_bundle(bundle)
        sha = digest(bundle)
        destination = store.root / "stacks" / (sha + ".tar")
        if destination.exists():
            if digest(destination) != sha:
                raise BenchError("Existing stack cache digest mismatch")
        else:
            os.chmod(bundle, 0o400)
            os.replace(bundle, destination)
        lock = {"schema": "kilix-agent-bench/stack-lock-v1", "benchmark_version": value["benchmark_version"],
                "captured_at": now(), "bundle": {"path": destination.name, "sha256": sha},
                "config": config, "sources": sources, "payload_bytes": total, "files": len(files)}
        lock_path = destination.with_suffix(".json")
        if lock_path.exists():
            existing, _, _, _ = load_lock(lock_path)
            if existing["config"] != config:
                raise BenchError("Existing lock configuration does not match")
        else:
            atomic_json(lock_path, lock)
        return {"status": "captured", "lock": str(lock_path), "sha256": sha, "files": len(files)}


def validate_bundle(path):
    """Reject substitutions, duplicates, traversal and links before mutation."""
    with tarfile.open(path, "r:") as archive:
        first = archive.next()
        if first is None or first.name != "manifest.json" or not first.isfile() or first.size > 32 * 1024**2:
            raise BenchError("Missing or oversized stack manifest")
        manifest = json.load(archive.extractfile(first))
        fields = {"schema", "benchmark_version", "config", "sources", "packages", "setup", "files", "directories"}
        _keys(manifest, fields, fields, "payload")
        if manifest["schema"] != "kilix-agent-bench/stack-payload-v1":
            raise BenchError("Unsupported stack payload schema")
        validate_config(manifest["config"])
        _steps(manifest["setup"])
        if not isinstance(manifest["sources"], list) or len(manifest["sources"]) > 1000:
            raise BenchError("Invalid source inventory")
        destinations = set()
        for source in manifest["sources"]:
            _keys(source, {"destination", "commit", "repository"}, {"destination", "commit", "repository"}, "source")
            relative(source["destination"])
            _sha(source["commit"], 40)
            _repository(source["repository"])
            if source["destination"] in destinations:
                raise BenchError("Duplicate source destination")
            destinations.add(source["destination"])
        entries = {}
        total = 0
        if not isinstance(manifest["files"], list) or len(manifest["files"]) > MAX_FILES:
            raise BenchError("Invalid or oversized file inventory")
        for entry in manifest["files"]:
            _keys(entry, {"path", "bytes", "sha256", "mode"}, {"path", "bytes", "sha256", "mode"}, "payload file")
            name = relative(entry["path"])
            if not name.startswith(("payload/", "packages/")) or name in entries:
                raise BenchError("Duplicate or invalid payload path")
            if type(entry["bytes"]) is not int or entry["bytes"] < 0 or entry["mode"] not in (0o644, 0o755):
                raise BenchError("Invalid payload file metadata")
            _sha(entry["sha256"])
            total += entry["bytes"]
            entries[name] = entry
        for name in entries:
            if any(str(p) in entries for p in PurePosixPath(name).parents):
                raise BenchError("A payload file cannot also be a directory")
        directories = manifest["directories"]
        if not isinstance(directories, list) or len(directories) > MAX_FILES:
            raise BenchError("Invalid or oversized directory inventory")
        seen_dirs = set()
        for name in directories:
            relative(name)
            if name.split('/')[0] not in ("payload", "packages") or name in seen_dirs:
                raise BenchError("Duplicate or invalid payload directory")
            if name in entries or any(str(p) in entries for p in PurePosixPath(name).parents):
                raise BenchError("Payload directory conflicts with a file")
            seen_dirs.add(name)
        if not isinstance(manifest["packages"], list):
            raise BenchError("Invalid package inventory")
        packages = {}
        for package in manifest["packages"]:
            _keys(package, {"path", "sha256", "identity"}, {"path", "sha256", "identity"}, "package identity")
            name = relative(package["path"])
            _sha(package["sha256"])
            if (not name.startswith("packages/") or not name.endswith(".deb") or name in packages
                    or name not in entries or entries[name]["sha256"] != package["sha256"]
                    or not isinstance(package["identity"], str) or not package["identity"]):
                raise BenchError("Package identity does not match its payload")
            packages[name] = package
        if set(packages) != {name for name in entries if name.startswith("packages/")}:
            raise BenchError("Package inventory is incomplete")
        if total > MAX_BYTES or len(entries) > MAX_FILES:
            raise BenchError("Oversized stack payload")
        seen = set()
        while (member := archive.next()) is not None:
            name = relative(member.name)
            if name not in entries or name in seen or not member.isfile():
                raise BenchError("Unlisted, duplicate or non-regular archive member")
            entry = entries[name]
            if member.size != entry["bytes"] or member.mode != entry["mode"]:
                raise BenchError("Archive member metadata mismatch")
            stream = archive.extractfile(member)
            h = hashlib.sha256()
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                h.update(chunk)
            if h.hexdigest() != entry["sha256"]:
                raise BenchError("Archive member digest mismatch")
            seen.add(name)
        if seen != entries.keys():
            raise BenchError("Stack archive is missing manifest members")
        return manifest


def load_lock(path):
    path = Path(path).resolve()
    lock = json.loads(path.read_text())
    _keys(lock, {"schema", "benchmark_version", "captured_at", "bundle", "config", "sources", "payload_bytes", "files"},
          {"schema", "benchmark_version", "bundle", "config"}, "stack lock")
    if lock["schema"] != "kilix-agent-bench/stack-lock-v1":
        raise BenchError("Unsupported stack lock schema")
    _keys(lock["bundle"], {"path", "sha256"}, {"path", "sha256"}, "bundle")
    _sha(lock["bundle"]["sha256"])
    bundle = path.parent / relative(lock["bundle"]["path"])
    if bundle.is_symlink() or digest(bundle) != lock["bundle"]["sha256"]:
        raise BenchError("Stack bundle digest mismatch")
    config = validate_config(lock["config"], path.parent)
    manifest = validate_bundle(bundle)
    if manifest["benchmark_version"] != lock["benchmark_version"] or manifest["config"] != config:
        raise BenchError("Stack benchmark version or configuration mismatch")
    if lock.get("sources") != manifest["sources"]:
        raise BenchError("Stack source inventory mismatch")
    if lock.get("files") != len(manifest["files"]) or lock.get("payload_bytes") != sum(f["bytes"] for f in manifest["files"]):
        raise BenchError("Stack payload inventory mismatch")
    return lock, bundle, manifest, config


def provision(vm, lock_path, progress=lambda message: None):
    with vm.operation():
        try:
            return _provision(vm, lock_path, progress)
        except BaseException as exc:
            if vm.state.get("stack", {}).get("phase") == "installing":
                vm.state["stack"].update(phase="failed", error=f"{type(exc).__name__}: {exc}")
                vm.save()
            raise


def _provision(vm, lock_path, progress):
    lock, bundle, manifest, config = load_lock(lock_path)
    if config != vm.state["config"]:
        raise BenchError("Guest configuration differs from the frozen stack configuration")
    if vm.state.get("stack"):
        raise BenchError("This generation already has a stack; reset before provisioning")
    guest_bundle = "/var/tmp/bench-stack.tar"
    vm.state["stack"] = {"sha256": lock["bundle"]["sha256"], "phase": "installing"}
    vm.save()
    progress("Uploading the frozen stack to the guest")
    receipt = vm.upload(bundle, guest_bundle)
    if receipt["sha256"] != lock["bundle"]["sha256"]:
        raise BenchError("Guest upload digest mismatch")
    # The same validator is run in the guest before extraction or package
    # installation. Ship our source as an observer-owned temporary module.
    sources = {name: (Path(__file__).parent / name).read_text() for name in ("core.py", "stack.py")}
    script = f"""import hashlib, json, os, pathlib, pwd, shutil, subprocess, sys, tarfile
stage = pathlib.Path('/var/lib/bench-stack')
stage.mkdir(mode=0o700)
module = stage / 'validator'
module.mkdir()
(module / '__init__.py').touch()
for name, source in {sources!r}.items():
    (module / name).write_text(source)
sys.path.insert(0, str(stage))
from validator.stack import validate_bundle
bundle = pathlib.Path({guest_bundle!r})
manifest = validate_bundle(bundle)
payload = stage / 'unpacked'
payload.mkdir(mode=0o700)
for directory in manifest['directories']:
    (payload / directory).mkdir(mode=0o700, parents=True, exist_ok=True)
with tarfile.open(bundle, 'r:') as archive:
    for member in archive.getmembers()[1:]:
        target = payload / member.name
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open('xb') as out, archive.extractfile(member) as src:
            shutil.copyfileobj(src, out)
        target.chmod(member.mode)
packages = sorted(str(p) for p in (payload / 'packages').glob('*.deb'))
if packages:
    environment = dict(os.environ, DEBIAN_FRONTEND='noninteractive')
    with (stage / 'packages.log').open('wb') as log:
        for args in (['dpkg', '--unpack', *packages], ['dpkg', '--configure', '--pending']):
            result = subprocess.run(args, stdout=log, stderr=subprocess.STDOUT, env=environment, timeout=600)
            if result.returncode:
                log.flush()
                raise RuntimeError('Offline package installation failed: ' + (stage / 'packages.log').read_text()[-2000:])
user = pwd.getpwnam('actor')
home = pathlib.Path(user.pw_dir)
destination = home / '.local/gpu_terminal'
destination.parent.mkdir(parents=True, exist_ok=True)
if destination.exists():
    raise RuntimeError('Guest stack destination is already occupied')
shutil.move(str(payload / 'payload'), destination)
for root, dirs, files in os.walk(home / '.local'):
    os.chown(root, user.pw_uid, user.pw_gid)
    os.chmod(root, 0o700)
    for name in files:
        os.chown(os.path.join(root, name), user.pw_uid, user.pw_gid)
environment = {{'HOME': str(home), 'USER': 'actor', 'LOGNAME': 'actor',
               'PATH': str(home / '.local/bin') + ':/usr/local/bin:/usr/bin:/bin',
               'LANG': 'C.UTF-8', 'KILIX_APP_AUTO_INSTALL': '0'}}
steps = []
for step in manifest['setup']:
    log = stage / (step['id'].replace('/', '-') + '.log')
    with log.open('wb') as output:
        result = subprocess.run(['runuser', '-u', 'actor', '--', 'env', '-i',
                                 *[key + '=' + value for key, value in environment.items()], *step['argv']],
                                cwd=home, stdout=output, stderr=subprocess.STDOUT, timeout=step['timeout_s'])
    steps.append({{'id': step['id'], 'exit': result.returncode, 'log': log.read_text(errors='replace')[-4000:]}})
    if result.returncode:
        raise RuntimeError('Setup failed: ' + json.dumps(steps[-1]))
inventory = subprocess.check_output(['dpkg-query', '-W', '-f=${{Package}}\\t${{Version}}\\t${{Architecture}}\\n'], text=True)
(stage / 'packages.tsv').write_text(inventory)
bundle.unlink()
shutil.rmtree(payload)
print(json.dumps({{'steps': steps, 'package_inventory': inventory, 'payload_files': len(manifest['files'])}}))
"""
    progress("Installing pinned packages and configuring the guest stack offline")
    result = json.loads(vm.ssh(script, timeout=1800).stdout)
    vm.state["stack"].update(phase="ready", benchmark_version=lock["benchmark_version"], provisioned_at=now())
    vm.save()
    return {"sha256": lock["bundle"]["sha256"], "benchmark_version": lock["benchmark_version"], **result}


def check_interfaces(vm, lock_path):
    """Observe real guest interfaces; all coding clients are process fixtures."""
    lock, _, manifest, _ = load_lock(lock_path)
    expected = "literal " + secrets.token_hex(12) + " $HOME ; 'quoted' λ"
    engine = next((f for f in manifest["files"]
                   if f["path"] == "payload/kilix/build/current/src/kitty/launcher/kitty"), None)
    if engine is None:
        raise BenchError("The canonical Kilix stack profile requires the built engine")
    request = {"sources": manifest["sources"], "engine_sha256": engine["sha256"], "literal": expected}
    source = (Path(__file__).parent / "guest_probe.py").read_text()
    observation = json.loads(vm.ssh("REQUEST = " + repr(request) + "\n" + source, timeout=420).stdout)
    required = {"sources_match", "engine_bytes_match", "engine_source_matches_launcher", "launcher_selects_frozen_engine",
                "engine_extension_loads", "lfm2_assets_ready", "grammar_assets_ready", "rollout_log_does_not_reinstall",
                "system_grammar_executes", "typing_does_not_submit", "worker_opens_one_pane",
                "worker_preserves_model_and_max", "team_opens_two_in_one_tab", "team_preserves_prompts",
                "unrelated_panes_preserved", "controller_context_bound", "team_preserves_models_and_efforts",
                "worker_and_team_cwd_preserved"}
    checks = observation.get("checks", {})
    transport_ok = (isinstance(checks, dict) and set(checks) == required
                    and all(value is True for value in checks.values()) and "error" not in observation)
    try:
        literal = base64.b64decode(observation.get("observations", {}).get("literal_base64", ""), validate=True)
    except (ValueError, TypeError) as exc:
        raise BenchError("Malformed literal-byte observation") from exc
    literal_ok = literal == expected.encode()
    return {"status": "passed" if transport_ok and literal_ok else "failed",
            "bundle_sha256": lock["bundle"]["sha256"], "observation": observation,
            "literal_bytes_match": literal_ok, "expected_literal_sha256": hashlib.sha256(expected.encode()).hexdigest(),
            "scope": "OS interfaces and model asset readiness; provider clients and model inference remain unqualified"}
