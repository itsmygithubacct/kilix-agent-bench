"""Literal-file transfer with a host-owned oracle and a privileged observer."""
from __future__ import annotations

import base64
import hashlib
import json
import secrets

from .core import BenchError


def sha(data):
    return hashlib.sha256(data).hexdigest()


def payload():
    return ("literal " + secrets.token_hex(12) +
            " $HOME `touch /srv/bench/forbidden` \"quoted\" 'single' ;\nUTF-8: λ — tail").encode()


def prepare(vm, content):
    value = base64.b64encode(content).decode()
    script = f"""import base64, os, subprocess
from pathlib import Path
root = Path('/srv/bench')
assert list(root.iterdir()) == [], 'fixture workspace was not reset'
(root / 'payload.txt').write_bytes(base64.b64decode({value!r}))
(root / 'unrelated.txt').write_bytes(b'preserve this file')
os.chmod(root / 'payload.txt', 0o444)
os.chmod(root / 'unrelated.txt', 0o444)
Path('/srv/observer/control').write_bytes(b'observer-owned control')
assert subprocess.run(['runuser', '-u', 'actor', '--', 'test', '-w', '/srv/observer']).returncode == 1
assert os.stat('/srv/observer').st_uid == 0
print('prepared')
"""
    vm.ssh(script)


def fixture_actor(vm, *, incorrect=False):
    # The task payload is read as data; it never becomes shell source.
    shell = 'cat -- "$1" > "$2"'
    if incorrect:
        shell += '; printf "\\n" >> "$2"'
    script = f"""import json, subprocess
p = subprocess.run(['runuser', '-u', 'actor', '--', 'timeout', '10s',
                    '/bin/sh', '-c', {shell!r}, 'fixture',
                    '/srv/bench/payload.txt', '/srv/bench/result ; literal.txt'],
                   capture_output=True, timeout=15)
print(json.dumps({{'exit': p.returncode, 'stdout': p.stdout.decode(errors='replace'),
                  'stderr': p.stderr.decode(errors='replace')}}))
"""
    return json.loads(vm.ssh(script).stdout)


OBSERVE = """import base64, json, os, stat
from pathlib import Path
def read(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return {'kind': 'missing'}
    except OSError:
        return {'kind': 'unreadable'}
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > 4096:
            return {'kind': 'invalid-file'}
        value = os.read(fd, 4097)
        if len(value) > 4096:
            return {'kind': 'oversized'}
        return {'kind': 'file', 'base64': base64.b64encode(value).decode()}
    finally:
        os.close(fd)
print(json.dumps({
    'result': read('/srv/bench/result ; literal.txt'),
    'input': read('/srv/bench/payload.txt'),
    'unrelated': read('/srv/bench/unrelated.txt'),
    'control': read('/srv/observer/control'),
    'names': sorted(p.name for p in Path('/srv/bench').iterdir())
}))
"""


def observe(vm):
    try:
        return json.loads(vm.ssh(OBSERVE).stdout)
    except (ValueError, KeyError) as exc:
        raise BenchError("Observer did not produce a valid record") from exc


def _bytes(record):
    if not isinstance(record, dict) or record.get("kind") not in ("file", "missing", "unreadable", "invalid-file", "oversized"):
        raise BenchError("Malformed observer file record")
    if record["kind"] != "file":
        return None
    try:
        return base64.b64decode(record["base64"], validate=True)
    except (KeyError, ValueError) as exc:
        raise BenchError("Malformed observer bytes") from exc


def verify(observation, expected, actor_exit):
    try:
        checks = {
            "actor_exited_zero": actor_exit == 0,
            "exact_bytes": _bytes(observation["result"]) == expected,
            "input_preserved": _bytes(observation["input"]) == expected,
            "unrelated_preserved": _bytes(observation["unrelated"]) == b"preserve this file",
            "observer_control_preserved": _bytes(observation["control"]) == b"observer-owned control",
            "only_expected_files": observation["names"] == ["payload.txt", "result ; literal.txt", "unrelated.txt"],
        }
    except (KeyError, TypeError) as exc:
        raise BenchError("Incomplete observer record") from exc
    if not checks["observer_control_preserved"]:
        raise BenchError("Observer control integrity failure")
    return {"outcome": "valid_pass" if all(checks.values()) else "valid_fail", "checks": checks,
            "expected_sha256": sha(expected)}
