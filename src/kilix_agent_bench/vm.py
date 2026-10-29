"""Local QEMU/KVM guests, pinned SSH identities and recoverable ownership."""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
from functools import wraps
import json
import os
from pathlib import Path
import platform
import shutil
import signal
import socket
import subprocess
import threading
import time
import uuid

from .core import (BenchError, acquire_image, atomic_json, command, digest,
                   now, private_dir, validate_config)


TOOLS = ("qemu-system-x86_64", "qemu-img", "ssh", "ssh-keygen", "xorriso")


def serialized(method):
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        with self.operation():
            return method(self, *args, **kwargs)
    return wrapped


def doctor(store, config, exclude_id=None):
    issues = []
    tools = {name: shutil.which(name) for name in TOOLS}
    issues.extend(f"Install {name}" for name, path in tools.items() if not path)
    if platform.system() != "Linux" or platform.machine() not in ("x86_64", "amd64"):
        issues.append("The initial backend requires Linux on x86-64")
    try:
        fd = os.open("/dev/kvm", os.O_RDWR | os.O_CLOEXEC)
        try:
            if fcntl.ioctl(fd, 0xAE00, 0) != 12:  # KVM_GET_API_VERSION
                issues.append("Unsupported KVM API")
        finally:
            os.close(fd)
    except OSError:
        issues.append("KVM must be available and accessible to this user")
    available = 0
    try:
        values = dict(line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines())
        available = int(values["MemAvailable"].split()[0]) * 1024
    except (OSError, KeyError, ValueError):
        issues.append("Cannot read available host memory")
    requested = config["vm"]
    if requested["cpus"] > (os.cpu_count() or 1):
        issues.append("Requested CPU count exceeds host CPU count")
    if available < (requested["memory_mib"] + 512) * 1024**2:
        issues.append("Insufficient available memory including a 512 MiB host reserve")
    reserved = 0
    for path in (store.root / "vms").glob("vm-*/state.json"):
        if path.parent.name == exclude_id:
            continue
        state = json.loads(path.read_text())
        if state.get("phase") != "destroyed":
            disk = path.parent / "disk.qcow2"
            used = disk.stat().st_blocks * 512 if disk.exists() else 0
            reserved += max(0, state["config"]["vm"]["disk_gib"] * 1024**3 - used)
    free = shutil.disk_usage(store.root).free
    required = (requested["disk_gib"] + 4) * 1024**3 + reserved
    if free < required:
        issues.append("Insufficient disk space for bounded disks, reservations and image staging")
    return {"schema": "kilix-agent-bench/doctor-v1", "ok": not issues, "issues": issues,
            "tools": tools, "available_memory_bytes": available, "free_disk_bytes": free,
            "reserved_disk_bytes": reserved, "required_disk_bytes": required,
            "acceleration": "kvm", "data_root": str(store.root)}


def process_info(pid):
    try:
        root = Path("/proc") / str(pid)
        fields = (root / "stat").read_text().rsplit(")", 1)[1].split()
        if fields[0] == "Z":
            return None
        opened = []
        for entry in (root / "fd").iterdir():
            try:
                opened.append(os.readlink(entry))
            except FileNotFoundError:
                continue
        return {"pid": pid, "start_ticks": fields[19],
                "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
                "exe": str((root / "exe").resolve(strict=True)),
                "cwd": str((root / "cwd").resolve(strict=True)),
                "open_files": opened,
                "argv": (root / "cmdline").read_bytes().decode().split("\0")[:-1]}
    except (FileNotFoundError, ProcessLookupError) as exc:
        # An individual proc entry can disappear while the process is alive
        # (for example an unlinked cwd). Only a missing process or a zombie
        # proves that cleanup may proceed without stopping it.
        try:
            remaining = (Path('/proc') / str(pid) / 'stat').read_text().rsplit(')', 1)[1].split()
            if remaining[0] == 'Z':
                return None
        except (FileNotFoundError, ProcessLookupError):
            return None
        except (OSError, ValueError, IndexError) as detail:
            raise BenchError(f"Cannot inspect process {pid}: {detail}") from detail
        raise BenchError(f"Cannot completely inspect live process {pid}: {exc}") from exc
    except (OSError, ValueError, IndexError) as exc:
        raise BenchError(f"Cannot inspect process {pid}: {exc}") from exc


def qmp(path, name):
    with socket.socket(socket.AF_UNIX) as sock:
        sock.settimeout(5)
        sock.connect(str(path))
        with sock.makefile("rwb", buffering=0) as stream:
            if "QMP" not in json.loads(stream.readline(65536)):
                raise BenchError("Invalid QMP greeting")
            for cmd in ("qmp_capabilities", name):
                stream.write(json.dumps({"execute": cmd}).encode() + b"\n")
                while True:
                    line = stream.readline(65536)
                    if not line:
                        raise BenchError("QMP closed before acknowledging the request")
                    result = json.loads(line)
                    if "error" in result:
                        raise BenchError(f"QMP request failed: {result['error']}")
                    if "return" in result:
                        break
            return result["return"]


class VM:
    def __init__(self, store, ident):
        self.store = store
        self.path = store.vm_dir(ident)
        self.ident = ident
        self._thread_lock = threading.RLock()
        self._operation_depth = 0
        if not (self.path / "state.json").is_file():
            raise BenchError(f"VM does not exist: {ident}")
        self.state = json.loads((self.path / "state.json").read_text())
        if self.state.get("id") != ident or self.state.get("schema") != "kilix-agent-bench/vm-v1":
            raise BenchError("Invalid VM state")
        validate_config(self.state["config"])

    @contextmanager
    def operation(self):
        with self._thread_lock:
            fd = None
            if not self._operation_depth:
                fd = os.open(self.store.root / "vms" / f".{self.ident}.lock",
                             os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    self.state = json.loads((self.path / "state.json").read_text())
                except BlockingIOError as exc:
                    os.close(fd)
                    raise BenchError("Another operation owns this VM; retry after it completes") from exc
                except BaseException:
                    os.close(fd)
                    raise
            self._operation_depth += 1
            try:
                yield
            finally:
                self._operation_depth -= 1
                if fd is not None:
                    os.close(fd)

    def save(self):
        self.state["updated_at"] = now()
        atomic_json(self.path / "state.json", self.state)

    @classmethod
    def create(cls, store, config):
        config = validate_config(config)
        with store.lock():
            report = doctor(store, config)
            if not report["ok"]:
                raise BenchError("; ".join(report["issues"]))
            base = acquire_image(store, config)
            ident = "vm-" + uuid.uuid4().hex[:12]
            path = private_dir(store.vm_dir(ident))
            if len(os.fsencode(path / "qmp.sock")) > 103:
                path.rmdir()
                raise BenchError("Data root is too long for a QMP Unix socket; choose a shorter root")
            state = {"schema": "kilix-agent-bench/vm-v1", "id": ident,
                     "uuid": str(uuid.uuid4()), "phase": "creating", "config": config,
                     "created_at": now(), "generation": 0, "process": None,
                     "qemu_exe": str(Path(report["tools"]["qemu-system-x86_64"]).resolve()),
                     "base_sha512": config["image"]["sha512"], "network": "restricted-user"}
            atomic_json(path / "state.json", state)
            vm = cls(store, ident)
            try:
                vm._seed()
                vm._disk(base)
                vm.state["phase"] = "stopped"
                vm.save()
            except BaseException:
                # No process is started by create; remove only this new directory.
                shutil.rmtree(path)
                raise
            return vm

    def _disk(self, base):
        command(["qemu-img", "create", "-f", "qcow2", "-F", "qcow2", "-b", str(base),
                 "disk.qcow2", f"{self.state['config']['vm']['disk_gib']}G"], cwd=self.path)
        os.chmod(self.path / "disk.qcow2", 0o600)

    def _seed(self):
        for key in ("client_key", "host_key"):
            command(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "fixture",
                     "-f", str(self.path / key)])
        cloud = {
            "users": [{"name": "root", "lock_passwd": True,
                       "ssh_authorized_keys": [(self.path / "client_key.pub").read_text().strip()]},
                      {"name": "actor", "lock_passwd": True, "shell": "/bin/sh", "sudo": None}],
            "disable_root": False, "ssh_pwauth": False,
            "ssh_keys": {"ed25519_private": (self.path / "host_key").read_text(),
                         "ed25519_public": (self.path / "host_key.pub").read_text().strip()},
            "runcmd": [["install", "-d", "-m", "0700", "-o", "actor", "-g", "actor", "/srv/bench"],
                       ["install", "-d", "-m", "0700", "/srv/observer"]],
        }
        # JSON is a YAML subset. No shell interpolation of key or configuration data.
        (self.path / "user-data").write_text("#cloud-config\n" + json.dumps(cloud) + "\n")
        os.chmod(self.path / "user-data", 0o600)
        (self.path / "meta-data").write_text(json.dumps({"instance-id": self.ident, "local-hostname": "bench-guest"}))
        command(["xorriso", "-as", "mkisofs", "-quiet", "-output", "seed.iso", "-volid", "cidata",
                 "-joliet", "-rock", "user-data", "meta-data"], cwd=self.path)
        os.chmod(self.path / "seed.iso", 0o600)
        self.state["seed_sha256"] = digest(self.path / "seed.iso")

    def owned_process(self):
        process = self.state.get("process")
        pidfile = self.path / "qemu.pid"
        if process:
            pid = process["pid"]
        elif pidfile.is_file():
            try:
                pid = int(pidfile.read_text().strip())
            except ValueError as exc:
                raise BenchError("Invalid QEMU pidfile") from exc
        else:
            return None
        current = process_info(pid)
        if current is None:
            return None
        args = current["argv"]
        try:
            # QEMU daemonization changes cwd to /. The open disk and unique
            # UUID/name remain bound to this guest across that transition.
            matches = (current["exe"] == self.state["qemu_exe"]
                       and (process is not None or str(self.path / "disk.qcow2") in current["open_files"])
                       and args[args.index("-uuid") + 1] == self.state["uuid"]
                       and args[args.index("-name") + 1] == self.ident)
        except (ValueError, IndexError):
            matches = False
        if process:
            matches = matches and all(current[k] == process[k] for k in ("start_ticks", "boot_id"))
        if not matches:
            raise BenchError("Process ownership mismatch; refusing to signal or remove VM files")
        return current

    @serialized
    def start(self):
        with self.store.lock():
            if self.owned_process():
                raise BenchError("VM is already running")
            if self.state["phase"] not in ("stopped", "boot_failed", "starting", "ready"):
                raise BenchError(f"VM cannot start from {self.state['phase']}")
            admission = doctor(self.store, self.state["config"], exclude_id=self.ident)
            if not admission["ok"]:
                raise BenchError("; ".join(admission["issues"]))
            acquire_image(self.store, self.state["config"])
            self.state["process"] = None
            for name in ("qemu.pid", "qmp.sock"):
                (self.path / name).unlink(missing_ok=True)
            with socket.socket() as port_socket:
                port_socket.bind(("127.0.0.1", 0))
                port = port_socket.getsockname()[1]
            self.state["port"] = port
            public = (self.path / "host_key.pub").read_text().split()
            (self.path / "known_hosts").write_text(f"[127.0.0.1]:{port} {public[0]} {public[1]}\n")
            cfg = self.state["config"]["vm"]
            argv = [self.state["qemu_exe"], "-name", self.ident, "-uuid", self.state["uuid"],
                    "-machine", "q35,accel=kvm", "-cpu", "host", "-smp", str(cfg["cpus"]),
                    "-m", str(cfg["memory_mib"]), "-display", "none", "-nodefaults", "-device", "VGA",
                    "-no-reboot", "-d", "guest_errors,cpu_reset", "-D", "qemu.log",
                    "-drive", "file=disk.qcow2,if=virtio,format=qcow2",
                    "-drive", "file=seed.iso,media=cdrom,readonly=on,format=raw",
                    "-device", "virtio-net-pci,netdev=net0", "-netdev",
                    f"user,id=net0,restrict=on,hostfwd=tcp:127.0.0.1:{port}-:22",
                    "-device", "virtio-rng-pci", "-qmp", "unix:qmp.sock,server=on,wait=off",
                    "-serial", "file:serial.log", "-pidfile", "qemu.pid", "-daemonize"]
            self.state.update(phase="starting", argv=argv, qemu_sha256=digest(self.state["qemu_exe"]))
            self.save()
            try:
                command(argv, cwd=self.path)
                self.state["process"] = self.owned_process()
                if not self.state["process"]:
                    raise BenchError("QEMU exited during startup")
                self.save()
            except BaseException:
                self.state["phase"] = "boot_failed"
                self.save()
                raise
        try:
            self.wait_ready()
        except BaseException:
            self.state["phase"] = "boot_failed"
            self.save()
            raise
        return self.state

    def ssh(self, script, *, timeout=30, check=True):
        if not self.owned_process():
            raise BenchError("Guest process is not running")
        argv = ["ssh", "-F", "/dev/null", "-i", str(self.path / "client_key"), "-p", str(self.state["port"]),
                "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes", "-o", "IdentityAgent=none",
                "-o", "StrictHostKeyChecking=yes", "-o", f"UserKnownHostsFile={self.path / 'known_hosts'}",
                "-o", "GlobalKnownHostsFile=/dev/null", "-o", "ConnectTimeout=2", "-o", "ConnectionAttempts=1",
                "-o", "ServerAliveInterval=5", "-o", "ServerAliveCountMax=2",
                "root@127.0.0.1", "python3 -"]
        try:
            result = subprocess.run(argv, input=script.encode(), capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            raise BenchError("Guest observation or execution timed out") from exc
        if check and result.returncode:
            raise BenchError(f"Guest transport/command exited {result.returncode}: {result.stderr.decode(errors='replace')[-1200:]}")
        return result

    def wait_ready(self):
        deadline = time.monotonic() + self.state["config"]["vm"]["boot_timeout_s"]
        script = "from pathlib import Path\nimport pwd\nassert Path('/var/lib/cloud/instance/boot-finished').exists()\npwd.getpwnam('actor')\nassert Path('/srv/observer').is_dir()\nprint('ready')\n"
        while time.monotonic() < deadline:
            result = self.ssh(script, timeout=6, check=False)
            if result.returncode == 0:
                status = self.ssh("import subprocess\np = subprocess.run(['cloud-init','status','--format=json'],capture_output=True,text=True)\nprint(p.stdout)\nraise SystemExit(p.returncode)\n", check=False)
                self.state["cloud_init"] = json.loads(status.stdout)
                self.save()
                if status.returncode:
                    raise BenchError("Cloud-init reported a non-clean status; see the saved cloud_init record")
                self.state.update(phase="ready", ready_at=now())
                self.save()
                return
            time.sleep(1)
        raise BenchError("Guest readiness deadline expired; inspect serial.log")

    @serialized
    def stop(self):
        current = self.owned_process()
        if current:
            # Bind a recovered process before QMP closes its disk descriptors.
            self.state["process"] = current
            self.save()
            try:
                qmp(self.path / "qmp.sock", "quit")
            except (OSError, ValueError, BenchError):
                pass
            deadline = time.monotonic() + 5
            while self.owned_process() and time.monotonic() < deadline:
                time.sleep(0.1)
            if self.owned_process():
                fd = os.pidfd_open(current["pid"])
                try:
                    confirmed = self.owned_process()
                    if confirmed is None or any(confirmed[k] != current[k] for k in ("pid", "start_ticks", "boot_id")):
                        raise BenchError("Process changed before cleanup")
                    signal.pidfd_send_signal(fd, signal.SIGKILL)
                finally:
                    os.close(fd)
                deadline = time.monotonic() + 5
                while self.owned_process() and time.monotonic() < deadline:
                    time.sleep(0.1)
                if self.owned_process():
                    raise BenchError("Guest process did not stop")
        self.state.update(phase="stopped", process=None)
        (self.path / "qemu.pid").unlink(missing_ok=True)
        (self.path / "qmp.sock").unlink(missing_ok=True)
        self.save()

    @serialized
    def reset(self):
        with self.store.lock():
            self.stop()
            base = acquire_image(self.store, self.state["config"])
            self.state["phase"] = "resetting"
            self.save()
            (self.path / "disk.qcow2").unlink()
            self._disk(base)
            self.state.update(phase="stopped", generation=self.state["generation"] + 1)
            self.save()
        return self.start()

    @serialized
    def destroy(self):
        with self.store.lock():
            self.stop()
            shutil.rmtree(self.path)

    @serialized
    def status(self):
        return {"id": self.ident, "recorded_phase": self.state["phase"],
                "running": self.owned_process() is not None, "generation": self.state["generation"]}
