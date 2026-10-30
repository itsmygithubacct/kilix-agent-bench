"""Native usage normalization; unknown values never become zero-cost wins."""
from .core import BenchError
from .telemetry import content_hash


def count(value):
    if value is None:
        return None
    if type(value) is not int or value < 0:
        raise BenchError("Usage counters must be nonnegative integers or null")
    return value


def response_usage(ident, usage, dialect, *, source=None, model=None):
    if dialect == "messages":
        uncached = count(usage.get("input_tokens"))
        cached = count(usage.get("cache_read_input_tokens", 0))
        writes = count(usage.get("cache_creation_input_tokens", 0))
        input_total = None if None in (uncached, cached, writes) else uncached + cached + writes
        output = count(usage.get("output_tokens"))
        reasoning = count((usage.get("output_tokens_details") or {}).get("thinking_tokens"))
    elif dialect == "chat":
        input_total = count(usage.get("prompt_tokens"))
        output = count(usage.get("completion_tokens"))
        cached = count((usage.get("prompt_tokens_details") or {}).get("cached_tokens"))
        reasoning = count((usage.get("completion_tokens_details") or {}).get("reasoning_tokens"))
        uncached = input_total - cached if input_total is not None and cached is not None else None
        writes = None
    else:
        input_total = count(usage.get("input_tokens"))
        output = count(usage.get("output_tokens"))
        cached = count(usage.get("cached_input_tokens", (usage.get("input_tokens_details") or {}).get("cached_tokens")))
        reasoning = count(usage.get("reasoning_output_tokens", (usage.get("output_tokens_details") or {}).get("reasoning_tokens")))
        uncached = input_total - cached if input_total is not None and cached is not None else None
        writes = None
    if uncached is not None and uncached < 0:
        raise BenchError("Cached input exceeds total input")
    if reasoning is not None and output is not None and reasoning > output:
        raise BenchError("Reasoning subset exceeds output total")
    return {"id": ident, "source": source, "model": model, "input_tokens": input_total,
            "cached_input_tokens": cached, "uncached_input_tokens": uncached,
            "cache_write_input_tokens": writes, "output_tokens": output, "reasoning_output_tokens": reasoning,
            "total_tokens": input_total + output if input_total is not None and output is not None else None,
            "coverage": "complete_totals" if input_total is not None and output is not None else "missing_usage"}


def aggregate(rows, *, expected_responses=None):
    unique = {}
    for row in rows:
        key = row["id"]
        if key in unique and {k: v for k, v in row.items() if k != "source"} != {k: v for k, v in unique[key].items() if k != "source"}:
            raise BenchError("Conflicting usage for one response ID")
        unique[key] = row
    rows = list(unique.values())
    complete = bool(rows) and all(r["coverage"] == "complete_totals" for r in rows)
    if expected_responses is not None and len(rows) != expected_responses:
        complete = False
    result = {"schema": "kilix-agent-bench/usage-v1", "response_count": len(rows),
              "expected_responses": expected_responses, "coverage": "complete_totals" if complete else "missing_usage",
              "accounting_version": "native-usage-v1", "records": rows}
    for key in ("input_tokens", "cached_input_tokens", "uncached_input_tokens", "cache_write_input_tokens",
                "output_tokens", "reasoning_output_tokens", "total_tokens"):
        values = [r[key] for r in rows]
        result[key] = sum(values) if values and all(v is not None for v in values) else None
    # No totals-based ranking is permitted when response coverage is incomplete.
    result["ranking_total_tokens"] = result["total_tokens"] if complete else None
    return result


def normalize_codex(events, rollouts=()):
    snapshots, observed_models, observed_efforts = [], set(), set()
    for index, event in enumerate(rollouts):
        payload = event.get("payload") or {}
        if event.get("type") == "turn_context":
            if payload.get("model"):
                observed_models.add(payload["model"])
            if payload.get("effort"):
                observed_efforts.add(payload["effort"])
        if event.get("type") == "event_msg" and payload.get("type") == "token_count":
            info = payload.get("info") or {}
            total = info.get("total_token_usage")
            if total and (not snapshots or snapshots[-1][1] != total):
                snapshots.append((index, total))
    rows, previous = [], {}
    if snapshots:
        for index, current in snapshots:
            delta = {}
            for key in ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens"):
                value = current.get(key)
                if value is not None:
                    count(value)
                    if value < previous.get(key, 0):
                        raise BenchError("Usage cumulative counter decreased in a fresh controller session")
                    delta[key] = value - previous.get(key, 0)
            if delta.get("input_tokens", 0) or delta.get("output_tokens", 0):
                rows.append(response_usage("snapshot-" + str(index), delta, "responses", source={"rollout_event": index}))
            previous = current
    else:
        for index, event in enumerate(events):
            if event.get("type") == "turn.completed" and isinstance(event.get("usage"), dict):
                rows.append(response_usage("turn-" + str(index), event["usage"], "responses", source={"event": index}))
    value = aggregate(rows)
    value["granularity"] = "cumulative_response_deltas" if snapshots else "turn_aggregate"
    value["observed_models"] = sorted(observed_models)
    value["observed_efforts"] = sorted(observed_efforts)
    value["identity_evidence"] = "client turn context; provider identity may not be exposed"
    return value


def normalize_messages(events, rollouts=()):
    messages, result, efforts, response_efforts, expected = {}, None, set(), {}, set()
    for source_name, records in (("event", events), ("rollout_event", rollouts)):
        snapshots = {}
        for index, event in enumerate(records):
            if event.get("type") == "assistant":
                message = event.get("message", {})
                ident = message.get("id")
                if not ident:
                    continue
                expected.add(ident)
                if source_name == "rollout_event":
                    resolved = {event[key] for key in ("effort", "perTurnEffort")
                                if isinstance(event.get(key), str) and event[key]}
                    efforts.update(resolved)
                    response_efforts.setdefault(ident, set()).update(resolved)
                if not isinstance(message.get("usage"), dict):
                    continue
                current = response_usage(ident, message["usage"], "messages", source={source_name: index}, model=message.get("model"))
                if ident in snapshots:
                    _check_message_snapshot(snapshots[ident], current)
                snapshots[ident] = current
            if source_name == "event" and event.get("type") == "result":
                result = event
        for ident, current in snapshots.items():
            if ident in messages:
                # Streamed blocks can carry an early output count. The saved
                # transcript receives the final delta, but can lag live stdout.
                _check_message_snapshot(messages[ident], current, monotonic=False)
                old_output = messages[ident]["output_tokens"]
                if old_output is not None and (current["output_tokens"] is None or current["output_tokens"] < old_output):
                    continue
            messages[ident] = current
    value = aggregate(list(messages.values()), expected_responses=len(expected))
    if result and isinstance(result.get("usage"), dict):
        total = response_usage("session-result", result["usage"], "messages")
        if not expected:
            value = aggregate([total])
            value["granularity"] = "session_aggregate"
        elif any(total[key] != value[key] for key in (
            "input_tokens", "cached_input_tokens", "cache_write_input_tokens", "output_tokens"
        )):
            value.update(coverage="unreconciled_totals", ranking_total_tokens=None)
        value["session_total"] = total
    value.setdefault("granularity", "message")
    value["observed_models"] = sorted({r["model"] for r in messages.values() if r["model"]})
    value["observed_efforts"] = sorted(efforts)
    value["effort_coverage"] = "complete" if expected and all(response_efforts.get(ident) for ident in expected) else "missing"
    value["identity_evidence"] = "native response models and resolved per-response effort in the saved transcript"
    return value


def _check_message_snapshot(previous, current, *, monotonic=True):
    for key in ("input_tokens", "cached_input_tokens", "cache_write_input_tokens", "model"):
        if previous[key] != current[key]:
            raise BenchError("One message ID has inconsistent input usage or model")
    if monotonic and previous["output_tokens"] is not None and current["output_tokens"] is not None and current["output_tokens"] < previous["output_tokens"]:
        raise BenchError("Message output counter decreased")


def normalize_grok(events, persisted_usage=None, session_metadata=None):
    # The client persists a session usage ledger even when its stdout omits usage.
    if persisted_usage is not None:
        value = persisted_usage
        if isinstance(value.get("session"), dict):
            value = value["session"]
        if isinstance(value.get("usage"), dict):
            value = value["usage"]
        if isinstance(value.get("totals"), dict):
            value = value["totals"]
        aliases = {"input_tokens": ("input_tokens", "inputTokens", "prompt_tokens"),
                   "output_tokens": ("output_tokens", "outputTokens", "completion_tokens"),
                   "cached_input_tokens": ("cached_input_tokens", "cachedInputTokens", "cachedReadTokens", "cached_tokens"),
                   "reasoning_output_tokens": ("reasoning_output_tokens", "reasoningTokens", "reasoning_tokens")}
        normalized = {key: next((value[name] for name in names if name in value), None) for key, names in aliases.items()}
        row = response_usage("session-ledger", normalized, "responses", source={"artifact": "grok-usage.json"})
        answer = aggregate([row])
        answer["granularity"] = "session_aggregate"
        answer["model_calls"] = count(value.get("modelCalls"))
        answer["observed_models"] = sorted(value.get("modelUsage", {}))
        client_models = {event["model"] for event in events
                         if event.get("type") == "system" and isinstance(event.get("model"), str)}
        metadata = session_metadata or {}
        session_id = (metadata.get("info") or {}).get("id")
        observed_sessions = {event["session_id"] for event in events if event.get("session_id")}
        if session_id and observed_sessions and observed_sessions != {session_id}:
            raise BenchError("Saved session metadata belongs to a different controller session")
        if metadata.get("current_model_id"):
            client_models.add(metadata["current_model_id"])
        answer["client_models"] = sorted(client_models)
        effort = metadata.get("reasoning_effort")
        answer["observed_efforts"] = [effort] if isinstance(effort, str) and effort else []
        answer["identity_evidence"] = "provider usage-ledger models; client model and effort recorded separately"
        if answer["model_calls"] == 0:
            answer.update(coverage="missing_usage", ranking_total_tokens=None)
        if value.get("totalTokens") is not None and count(value["totalTokens"]) != row["total_tokens"]:
            answer.update(coverage="unreconciled_totals", ranking_total_tokens=None)
        turns = persisted_usage.get("turns")
        if turns and all("inputTokens" in t and "outputTokens" in t for t in turns):
            if any(sum(count(t[key]) for t in turns) != value.get(key) for key in ("inputTokens", "outputTokens")):
                answer.update(coverage="unreconciled_totals", ranking_total_tokens=None)
        return answer
    rows = []
    for index, event in enumerate(events):
        if isinstance(event.get("usage"), dict):
            raw = event["usage"]
            dialect = "chat" if "prompt_tokens" in raw else "responses"
            rows.append(response_usage(event.get("requestId", event.get("id", "event-" + str(index))), raw,
                                       dialect, source={"event": index}, model=event.get("model")))
    return aggregate(rows)
