"""Durable private events and monotonic timing for interrupted work too."""
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import time
import uuid

from .core import BenchError, private_dir


ACTIVE_SPAN = ContextVar("bench_active_span", default=None)


def timestamp():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def content_hash(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def read_events(path):
    path = Path(path)
    if not path.exists():
        return []
    if path.is_symlink():
        raise BenchError("Event file must not be a symlink")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "r") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_SH)
            return [json.loads(line) for line in stream if line.strip()]
    except (ValueError, UnicodeError) as exc:
        raise BenchError("Incomplete or malformed event journal; preserve it for recovery") from exc


def append_event(path, value, *, unique_key=None):
    """Locked, fsynced append. Re-importing the same identity is idempotent."""
    path = Path(path)
    private_dir(path.parent)
    wire = canonical(value) + b"\n"
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        st = os.fstat(fd)
        if st.st_uid != os.getuid() or st.st_mode & 0o077:
            raise BenchError("Event journal must be private and owned by this user")
        if unique_key:
            with os.fdopen(os.dup(fd), "r") as stream:
                stream.seek(0)
                for line in stream:
                    existing = json.loads(line)
                    if existing.get(unique_key) == value.get(unique_key):
                        if canonical(existing) != canonical(value):
                            raise BenchError("Conflicting record for an existing event identity")
                        return False
        offset = 0
        while offset < len(wire):
            offset += os.write(fd, wire[offset:])
        os.fsync(fd)
        return True
    finally:
        os.close(fd)


class Timings:
    def __init__(self, path, **context):
        self.path, self.context = Path(path), context

    @contextmanager
    def span(self, phase, *, parent_id=None, **metadata):
        ident = "span-" + uuid.uuid4().hex
        start = timestamp()
        begin = time.monotonic()
        common = {**self.context, **metadata, "span_id": ident, "parent_id": parent_id,
                  "phase": phase, "started_at": start, "clock": "monotonic"}
        if parent_id is None:
            common["parent_id"] = ACTIVE_SPAN.get()
        append_event(self.path, {**common, "event": "start"})
        token = ACTIVE_SPAN.set(ident)
        status = "completed"
        try:
            yield ident
        except BaseException:
            status = "interrupted_or_failed"
            raise
        finally:
            ACTIVE_SPAN.reset(token)
            append_event(self.path, {**common, "event": "end", "status": status,
                                    "completed_at": timestamp(), "elapsed_s": time.monotonic() - begin})


def summarize_timings(events):
    """Do not add nested spans or overlapping trials into an invented makespan."""
    starts = {e["span_id"]: e for e in events if e.get("event") == "start"}
    ended = {e["span_id"]: e for e in events if e.get("event") == "end"}
    groups = {}
    for event in ended.values():
        key = (event.get("track"), event.get("configuration_id"), event["phase"])
        groups.setdefault(key, []).append(event["elapsed_s"])
    output = []
    for key, values in sorted(groups.items(), key=lambda item: str(item[0])):
        values.sort()
        n = len(values)
        output.append({"track": key[0], "configuration_id": key[1], "phase": key[2], "n": n,
                       "min_s": values[0], "max_s": values[-1],
                       "median_s": (values[(n - 1) // 2] + values[n // 2]) / 2,
                       "p90_s": values[min(n - 1, (9 * n + 9) // 10 - 1)] if n >= 10 else None})
    return {"groups": output, "unfinished_span_ids": sorted(set(starts) - set(ended)),
            "interpretation": "Phase distributions; nested and concurrent spans are not additive."}
