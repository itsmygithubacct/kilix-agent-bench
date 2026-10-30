"""Isolated native clients and a bounded self-hosted chat/function-call driver."""
import json
import os
from pathlib import Path
import re
import selectors
import shutil
import signal
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from .bridge import Bridge, EXECUTE_TOOL
from .core import BenchError, atomic_json, digest, private_dir
from .telemetry import append_event, content_hash, timestamp
from .usage import aggregate, normalize_codex, normalize_grok, normalize_messages, response_usage


SYSTEM = "Complete the user's bounded OS task using the supplied VM command tool. Follow the required route and final-answer format."


def executable(client):
    command = shutil.which(client)
    if not command:
        raise BenchError("Controller client is not installed: " + client)
    path = Path(command).resolve()
    if client == "codex" and path.suffix == ".js":
        candidates = list(path.parent.parent.glob("node_modules/@openai/*/vendor/*/bin/codex"))
        candidates += list(path.parent.parent.glob("vendor/*/bin/codex"))
        if len(candidates) != 1:
            raise BenchError("Cannot uniquely resolve the installed native controller executable")
        path = candidates[0]
    return path


def native_layout(path, client, configuration, limits):
    """Mount a fresh home at its normal path; preserve the operator's environment."""
    operator_home = Path.home()
    client_home = private_dir(path / "client-home")
    work = private_dir(path / "client-work")
    ipc = private_dir(path / "ipc")
    (work / "policy.txt").write_text(SYSTEM)
    (work / "policy.txt").chmod(0o600)
    config_dir = private_dir(client_home / ("." + client))
    proxy_args = ["-m", "kilix_agent_bench.bridge", "/bench/ipc/bridge.sock"]
    common_mcp = '\n[mcp_servers.bench]\ncommand="/usr/bin/python3"\nargs=' + json.dumps(proxy_args) + '\nenv={PYTHONPATH="/bench/lib"}\nstartup_timeout_sec=20\ntool_timeout_sec=110\n'
    if client == "codex":
        config = '\napproval_policy="never"\nsandbox_mode="read-only"\nweb_search="disabled"\nproject_doc_max_bytes=0\nmodel_instructions_file="/bench/work/policy.txt"\n'
        config += 'model_reasoning_effort=' + json.dumps(configuration["effort"]) + '\n'
        config += '[features]\nshell_tool=false\nmulti_agent=false\napps=false\nplugins=false\njs_repl=false\nview_image=false\nimage_generation=false\ncomputer_use=false\nhooks=false\nskill_search=false\nskip_host_skill_discovery=true\n'
        config += common_mcp + 'required=true\nenabled_tools=["execute"]\n[mcp_servers.bench.tools.execute]\napproval_mode="approve"\n'
        (config_dir / "config.toml").write_text(config)
        args = ["exec", "--json", "--skip-git-repo-check", "--sandbox", "read-only", "-m", configuration["model"],
                "-C", "/bench/work", "-o", "/bench/work/final.txt", "-"]
        auth = operator_home / ".codex/auth.json"
    elif client == "grok":
        config = '[cli]\nauto_update=false\n[features]\ntelemetry=false\nfeedback=false\nlsp_tools=false\ncodebase_indexing=false\n[session]\nload_envrc=false\n[telemetry]\ntrace_upload=false\n' + common_mcp
        (config_dir / "config.toml").write_text(config)
        args = ["--prompt-file", "/bench/work/prompt.txt", "--model", configuration["model"],
                "--reasoning-effort", configuration["effort"], "--tools", "", "--no-plan", "--no-subagents",
                "--disable-web-search", "--always-approve", "--max-turns", str(limits["max_model_turns"]),
                "--system-prompt-override", SYSTEM, "--output-format", "streaming-messages-json",
                "--cwd", "/bench/work", "--leader-socket", str(operator_home / ".grok/leader-bench.sock")]
        auth = operator_home / ".grok/auth.json"
    elif client == "claude":
        atomic_json(client_home / ".claude.json", {"hasCompletedOnboarding": True})
        atomic_json(work / "mcp.json", {"mcpServers": {"bench": {"command": "/usr/bin/python3",
                    "args": proxy_args, "env": {"PYTHONPATH": "/bench/lib"}}}})
        args = ["--print", "--verbose", "--output-format", "stream-json", "--model", configuration["model"],
                "--effort", configuration["effort"], "--tools", "", "--strict-mcp-config", "--mcp-config", "/bench/work/mcp.json",
                "--setting-sources", "", "--disable-slash-commands", "--no-chrome", "--system-prompt", SYSTEM,
                "--permission-mode", "dontAsk", "--allowedTools", "mcp__bench__execute", "--max-turns", str(limits["max_model_turns"])]
        auth = operator_home / ".claude/.credentials.json"
    else:
        raise BenchError("Unsupported native controller client")
    if not auth.is_file():
        raise BenchError("Native client credential file is unavailable: " + str(auth))
    binary = executable(client)
    auth_target = client_home / auth.relative_to(operator_home)
    auth_target.touch(mode=0o600)
    for config_file in config_dir.glob("*"):
        config_file.chmod(0o600)
    mounted_binary = "/bench/native/" + binary.name
    command = ["bwrap", "--die-with-parent", "--new-session", "--unshare-user", "--unshare-pid", "--unshare-ipc", "--unshare-uts",
               "--ro-bind", "/usr", "/usr", "--symlink", "usr/bin", "/bin", "--symlink", "usr/lib", "/lib",
               "--symlink", "usr/lib64", "/lib64", "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp",
               "--bind", str(client_home), str(operator_home), "--bind", str(work), "/bench/work",
               "--bind", str(ipc), "/bench/ipc", "--ro-bind", str(Path(__file__).parent.parent), "/bench/lib",
               "--ro-bind", str(binary.parent), "/bench/native", "--ro-bind", str(auth), str(auth),
               "--chdir", "/bench/work"]
    for system_path in ("/etc/ssl", "/etc/resolv.conf", "/etc/hosts", "/etc/passwd", "/etc/group", "/etc/nsswitch.conf", "/etc/localtime"):
        if Path(system_path).exists():
            command += ["--ro-bind", system_path, system_path]
    command += [mounted_binary, *args]
    environment = {"HOME": str(operator_home), "USER": os.environ.get("USER", ""), "LOGNAME": os.environ.get("LOGNAME", ""),
                   "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "TERM": "dumb", "NO_COLOR": "1",
                   "DISABLE_AUTOUPDATER": "1", "DISABLE_TELEMETRY": "1", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"}
    for key in ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "NO_PROXY", "https_proxy", "http_proxy", "no_proxy", "SSL_CERT_FILE", "SSL_CERT_DIR"):
        if key in os.environ:
            environment[key] = os.environ[key]
    return {"command": command, "environment": environment, "work": work, "client_home": client_home,
            "ipc": ipc, "binary_sha256": digest(binary), "binary": str(binary), "mounted_binary": mounted_binary}


def stream_process(command, environment, prompt, path, timeout_s, *, cancelled=None, stop_check=None):
    """Bound wall time and raw output; terminate the entire owned process group."""
    events, pending = [], b""
    with (path / "client-stdout.jsonl").open("wb") as stdout, (path / "client-stderr.log").open("wb") as stderr:
        os.chmod(stdout.name, 0o600)
        os.chmod(stderr.name, 0o600)
        p = subprocess.Popen(command, env=environment, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             start_new_session=True)
        selector = selectors.DefaultSelector()
        selector.register(p.stdout, selectors.EVENT_READ, "stdout")
        selector.register(p.stderr, selectors.EVENT_READ, "stderr")
        began = time.monotonic()
        total, timed_out, last_budget_check = 0, False, 0.0
        try:
            p.stdin.write(prompt.encode())
            p.stdin.close()
            while selector.get_map():
                budget_stop = False
                if stop_check and time.monotonic() - last_budget_check >= 0.5:
                    budget_stop = stop_check(events)
                    last_budget_check = time.monotonic()
                if (cancelled is not None and cancelled.is_set()) or budget_stop or time.monotonic() - began > timeout_s or total > 16 * 1024 * 1024:
                    timed_out = True
                    os.killpg(p.pid, signal.SIGKILL)
                    break
                for key, _ in selector.select(timeout=0.2):
                    data = os.read(key.fileobj.fileno(), 65536)
                    if not data:
                        selector.unregister(key.fileobj)
                        continue
                    total += len(data)
                    target = stdout if key.data == "stdout" else stderr
                    target.write(data)
                    target.flush()
                    if key.data == "stdout":
                        pending += data
                        while b"\n" in pending:
                            line, pending = pending.split(b"\n", 1)
                            try:
                                event = json.loads(line)
                                if isinstance(event, dict):
                                    events.append(event)
                            except ValueError:
                                pass
            p.wait(timeout=10)
        finally:
            if p.poll() is None:
                os.killpg(p.pid, signal.SIGKILL)
                p.wait(timeout=10)
            selector.close()
            p.stdout.close()
            p.stderr.close()
    return events, p.returncode, timed_out


def native_client(configuration, prompt, execute, path, timings, limits, *, cancelled=None):
    try:
        return _native_client(configuration, prompt, execute, path, timings, limits, cancelled=cancelled)
    finally:
        # Never finalize account/client state, including on parsing errors or interruption.
        state = Path(path) / "client-home"
        if state.exists():
            shutil.rmtree(state)


def _native_client(configuration, prompt, execute, path, timings, limits, *, cancelled=None):
    path = Path(path)
    client = configuration["client"]
    layout = native_layout(path, client, configuration, limits)
    (layout["work"] / "prompt.txt").write_text(prompt)
    (layout["work"] / "prompt.txt").chmod(0o600)
    atomic_json(path / "client-launch.json", {"argv": layout["command"], "binary": layout["binary"],
                "binary_sha256": layout["binary_sha256"], "configuration": configuration,
                "prompt_sha256": content_hash(prompt), "system_sha256": content_hash(SYSTEM),
                "tool_schema_sha256": content_hash(EXECUTE_TOOL), "auth": "existing credential mounted read-only outside guest"})
    config_file = layout["client_home"] / ("." + client) / "config.toml"
    if config_file.exists():
        saved_config = path / "client-config.toml"
        saved_config.write_bytes(config_file.read_bytes())
        saved_config.chmod(0o600)

    def stop_check(events):
        if cancelled is not None and cancelled.is_set():
            return True
        if client == "codex":
            records = []
            for source in sorted((layout["client_home"] / ".codex/sessions").rglob("*.jsonl")):
                for line in source.read_text().splitlines():
                    try:
                        records.append(json.loads(line))
                    except ValueError:
                        pass  # the writer may currently be appending this line
            value = normalize_codex(events, records)
        elif client == "claude":
            value = normalize_messages(events)
        else:
            ledgers = list((layout["client_home"] / ".grok/sessions").glob("**/usage.json"))
            persisted = None
            if len(ledgers) == 1:
                try:
                    persisted = json.loads(ledgers[0].read_text())
                except ValueError:
                    pass  # an in-progress write is not a complete usage observation
            value = normalize_grok(events, persisted)
        tokens = value.get("ranking_total_tokens")
        calls = value.get("model_calls") or value["response_count"]
        return (tokens is not None and tokens >= limits["max_controller_tokens"]) or calls >= limits["max_model_turns"]
    with Bridge(layout["ipc"] / "bridge.sock", execute), timings.span("controller_session", configuration_id=configuration["id"]):
        events, exit_code, timed_out = stream_process(layout["command"], layout["environment"],
                                                      "" if client == "grok" else prompt, path, limits["trial_timeout_s"],
                                                      cancelled=cancelled, stop_check=stop_check)
    answer = ""
    if client == "codex":
        final = layout["work"] / "final.txt"
        if final.exists():
            answer = final.read_text()
        rollouts = []
        for source in sorted((layout["client_home"] / ".codex/sessions").rglob("*.jsonl")):
            for line in source.read_text().splitlines():
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                rollouts.append(row)
                append_event(path / "client-rollout.jsonl", row)
        usage = normalize_codex(events, rollouts)
    elif client == "claude":
        for event in events:
            if event.get("type") == "result":
                answer = event.get("result", "")
        usage = normalize_messages(events)
    else:
        for event in events:
            if event.get("type") == "result":
                answer = event.get("result", event.get("text", ""))
            elif event.get("type") == "assistant":
                contents = event.get("message", {}).get("content", [])
                text = "".join(v.get("text", "") for v in contents if isinstance(v, dict) and v.get("type") == "text")
                if text:
                    answer = text
        persisted = None
        ledgers = list((layout["client_home"] / ".grok/sessions").glob("**/usage.json"))
        if len(ledgers) > 1:
            raise BenchError("A fresh single-controller session produced multiple usage ledgers")
        if ledgers:
            persisted = json.loads(ledgers[0].read_text())
            atomic_json(path / "grok-usage.json", persisted)
        usage = normalize_grok(events, persisted)
    atomic_json(path / "usage.json", usage)
    # Auth is a read-only mount and was never copied. Remove mutable client state after retaining evidence.
    shutil.rmtree(layout["client_home"])
    return {"answer": answer, "exit_code": exit_code, "timed_out": timed_out, "usage": usage,
            "client_binary_sha256": layout["binary_sha256"], "requested": configuration}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise BenchError("Inference redirects are refused; use the pinned endpoint directly")


def validate_endpoint(configuration, endpoint):
    if set(endpoint) - {"url", "key_env", "configuration_sha256", "runtime", "served_model"}:
        raise BenchError("Unknown endpoint fields; credentials must use a key_env reference")
    if endpoint.get("configuration_sha256") != content_hash(configuration):
        raise BenchError("Inference endpoint is not pinned to this controller configuration")
    runtime = endpoint.get("runtime", {})
    if not re.fullmatch(r".+@sha256:[0-9a-f]{64}", runtime.get("image", "")):
        raise BenchError("Inference runtime requires an exact container-image digest")
    for key in ("repository_revision", "precision", "context_tokens"):
        if runtime.get(key) != configuration.get(key) or configuration.get(key) is None:
            raise BenchError("Inference runtime differs from the frozen " + key)
    if not isinstance(runtime.get("launch_argv"), list) or not runtime["launch_argv"] or not all(isinstance(v, str) for v in runtime["launch_argv"]):
        raise BenchError("Inference runtime must record parser/template launch arguments")
    if not isinstance(endpoint.get("served_model"), str) or not endpoint["served_model"]:
        raise BenchError("An explicit served-model identity is required")
    parsed = urllib.parse.urlsplit(endpoint["url"])
    if not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise BenchError("Inference URL must not embed credentials, query or fragment")
    if parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost", "::1"}):
        raise BenchError("Inference requires HTTPS or an authenticated loopback tunnel")


def self_hosted_client(configuration, prompt, execute, path, timings, limits, endpoint, *, cancelled=None):
    validate_endpoint(configuration, endpoint)
    atomic_json(Path(path) / "endpoint.json", endpoint)
    opener = urllib.request.build_opener(NoRedirect())
    headers = {"Content-Type": "application/json"}
    if endpoint.get("key_env"):
        key = os.environ.get(endpoint["key_env"])
        if not key:
            raise BenchError("Configured inference key environment variable is unset")
        headers["Authorization"] = "Bearer " + key
    messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}]
    tool = {"type": "function", "function": {"name": "execute", "description": EXECUTE_TOOL["description"], "parameters": EXECUTE_TOOL["inputSchema"]}}
    rows, answer, models, completed = [], "", set(), False
    began = time.monotonic()
    settings = configuration.get("settings")
    if not isinstance(settings, dict):
        raise BenchError("Self-hosted controller settings must be frozen before execution")
    allowed_settings = {"temperature", "top_p", "top_k", "min_p", "presence_penalty", "repetition_penalty", "chat_template_kwargs", "reasoning_effort"}
    if set(settings) - allowed_settings:
        raise BenchError("Unsupported self-hosted sampling/template settings")
    for turn in range(limits["max_model_turns"]):
        if (cancelled is not None and cancelled.is_set()) or time.monotonic() - began >= limits["trial_timeout_s"]:
            break
        body = {"model": endpoint["served_model"], "messages": messages, "tools": [tool], "stream": False,
                "max_tokens": limits["max_output_tokens"], **settings}
        atomic_json(Path(path) / f"request-{turn:03d}.json", body)
        with timings.span("model_request", configuration_id=configuration["id"], turn=turn):
            request = urllib.request.Request(endpoint["url"].rstrip("/") + "/chat/completions", data=json.dumps(body).encode(), headers=headers)
            try:
                with opener.open(request, timeout=max(0.1, min(limits["request_timeout_s"], limits["trial_timeout_s"] - (time.monotonic() - began)))) as response:
                    raw = response.read(8 * 1024 * 1024 + 1)
                    if len(raw) > 8 * 1024 * 1024:
                        raise BenchError("Inference response exceeded the bound")
            except (urllib.error.URLError, TimeoutError) as exc:
                raise BenchError("Inference request failed; raw credentials are not logged") from exc
        result = json.loads(raw)
        atomic_json(Path(path) / f"response-{turn:03d}.json", result)
        model = result.get("model")
        if model != endpoint["served_model"]:
            raise BenchError("Inference response served a different or unreported model")
        if model:
            models.add(model)
        rows.append(response_usage(result.get("id", "response-" + str(turn)), result.get("usage") or {}, "chat",
                                   source={"artifact": f"response-{turn:03d}.json"}, model=model))
        message = result["choices"][0]["message"]
        messages.append(message)
        calls = message.get("tool_calls") or []
        if not calls:
            answer, completed = message.get("content") or "", True
            break
        usage = aggregate(rows)
        if usage["ranking_total_tokens"] is None or usage["total_tokens"] >= limits["max_controller_tokens"]:
            break
        for call in calls:
            function = call["function"]
            if function["name"] != "execute":
                output = {"exit_code": 2, "stderr": "Unknown tool"}
            else:
                try:
                    arguments = json.loads(function["arguments"])
                except ValueError:
                    arguments = None
                output = execute(arguments)
            messages.append({"role": "tool", "tool_call_id": call["id"], "content": json.dumps(output)})
    usage = aggregate(rows)
    usage["observed_models"] = sorted(models)
    atomic_json(Path(path) / "usage.json", usage)
    return {"answer": answer, "exit_code": 0 if completed else 2, "timed_out": not completed,
            "usage": usage, "requested": configuration}
