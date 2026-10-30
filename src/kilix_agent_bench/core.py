"""Private storage, validated configuration and immutable input acquisition."""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request


class BenchError(Exception):
    """A configuration, infrastructure or integrity error; never a task score."""


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def digest(path, algorithm="sha256"):
    h = hashlib.new(algorithm)
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def atomic_json(path, value):
    path = Path(path)
    fd, tmp = tempfile.mkstemp(prefix=".record-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
        dfd = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    finally:
        Path(tmp).unlink(missing_ok=True)


def command(argv, *, cwd=None, input=None, timeout=30):
    try:
        result = subprocess.run(argv, cwd=cwd, input=input, capture_output=True,
                                timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BenchError(f"{argv[0]} could not complete: {exc}") from exc
    if result.returncode:
        detail = result.stderr.decode(errors="replace")[-2000:]
        raise BenchError(f"{argv[0]} exited {result.returncode}: {detail}")
    return result.stdout


def private_dir(path):
    path = Path(path)
    if path.is_symlink():
        raise BenchError(f"Refusing symlink directory: {path}")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    st = path.stat()
    if st.st_uid != os.getuid() or st.st_mode & 0o077:
        raise BenchError(f"Directory must belong to this user and have mode 0700: {path}")
    return path


class Store:
    def __init__(self, root=None):
        selected = Path(root).expanduser() if root else Path.home() / ".local/gpu_terminal/kilix-agent-bench"
        if selected.is_symlink():
            raise BenchError("The data root must not be a symlink")
        self.root = selected.resolve()
        if self.root in (Path("/"), Path.home().resolve()):
            raise BenchError("Use a dedicated data directory")
        private_dir(self.root)
        for name in ("images", "stacks", "vms", "studies", "mining", "runs", "reports", "cache", "datasets", "rentals"):
            private_dir(self.root / name)

    @contextmanager
    def lock(self):
        fd = os.open(self.root / "lifecycle.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    def vm_dir(self, ident):
        if not re.fullmatch(r"vm-[0-9a-f]{12}", ident):
            raise BenchError("Invalid VM ID")
        path = self.root / "vms" / ident
        if path.is_symlink():
            raise BenchError("VM directory must not be a symlink")
        return path


def _keys(obj, allowed, required, where):
    if not isinstance(obj, dict) or set(obj) - set(allowed) or set(required) - set(obj):
        raise BenchError(f"Invalid or unknown fields in {where}; expected {sorted(allowed)}")


def validate_config(value, base=Path(".")):
    _keys(value, {"schema", "image", "vm"}, {"schema", "image", "vm"}, "configuration")
    if value["schema"] != "kilix-agent-bench/config-v1":
        raise BenchError("Unsupported configuration schema")
    image = value["image"]
    _keys(image, {"url", "path", "sha512"}, {"sha512"}, "image")
    if ("url" in image) == ("path" in image):
        raise BenchError("Specify exactly one image url or path")
    if not isinstance(image["sha512"], str) or not re.fullmatch(r"[0-9a-f]{128}", image["sha512"]):
        raise BenchError("An exact lowercase image SHA-512 is required")
    normalized = json.loads(json.dumps(value))
    if "url" in image:
        if not isinstance(image["url"], str):
            raise BenchError("Image URL must be a string")
        parsed = urllib.parse.urlsplit(image["url"])
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.fragment or parsed.query:
            raise BenchError("Image URL must use HTTPS without credentials, query or fragment")
    else:
        if not isinstance(image["path"], str) or not image["path"]:
            raise BenchError("Image path must be a nonempty string")
        normalized["image"]["path"] = str((base / Path(image["path"]).expanduser()).resolve())
    vm = value["vm"]
    limits = {"cpus": (1, 256), "memory_mib": (512, 262144), "disk_gib": (3, 2048),
              "boot_timeout_s": (30, 1800)}
    _keys(vm, limits, limits, "vm")
    for key, (low, high) in limits.items():
        if type(vm[key]) is not int or not low <= vm[key] <= high:
            raise BenchError(f"{key} must be an integer in {low}..{high}")
    return normalized


def load_config(path):
    path = Path(path)
    try:
        return validate_config(json.loads(path.read_text()), path.resolve().parent)
    except (OSError, ValueError) as exc:
        raise BenchError(f"Cannot load configuration: {exc}") from exc


def inspect_image(path, disk_gib):
    info = json.loads(command(["qemu-img", "info", "--output=json", "-f", "qcow2", str(path)]))
    data = info.get("format-specific", {}).get("data", {})
    if info.get("backing-filename") or data.get("data-file") or info.get("encrypted"):
        raise BenchError("Base images must be standalone, unencrypted qcow2 files")
    if info.get("virtual-size", 0) > disk_gib * 1024**3:
        raise BenchError("Configured disk is smaller than the base image")
    return info


def acquire_image(store, config):
    image = config["image"]
    path = store.root / "images" / (image["sha512"] + ".qcow2")
    if path.is_symlink():
        raise BenchError("Cached image must not be a symlink")
    if not path.exists():
        fd, raw = tempfile.mkstemp(prefix="image-", dir=store.root / "cache")
        tmp = Path(raw)
        try:
            with os.fdopen(fd, "wb") as out:
                source = (urllib.request.urlopen(image["url"], timeout=30) if "url" in image
                          else Path(image["path"]).open("rb"))
                with source:
                    if "url" in image and urllib.parse.urlsplit(source.geturl()).scheme != "https":
                        raise BenchError("Image redirect left HTTPS")
                    size, started = 0, time.monotonic()
                    for chunk in iter(lambda: source.read(1024 * 1024), b""):
                        size += len(chunk)
                        if size > 4 * 1024**3 or time.monotonic() - started > 900:
                            raise BenchError("Image download exceeded 4 GiB or 900 seconds")
                        out.write(chunk)
                out.flush()
                os.fsync(out.fileno())
            if digest(tmp, "sha512") != image["sha512"]:
                raise BenchError("Image checksum mismatch; cache entry was not installed")
            inspect_image(tmp, config["vm"]["disk_gib"])
            os.chmod(tmp, 0o400)
            os.replace(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)
    if digest(path, "sha512") != image["sha512"]:
        raise BenchError("Cached image checksum mismatch")
    inspect_image(path, config["vm"]["disk_gib"])
    return path
