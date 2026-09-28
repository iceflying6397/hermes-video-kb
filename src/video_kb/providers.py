"""One no-tools model request through the user's already selected Hermes API.

This module never constructs an agent, executes a model tool, loads a Skill, or
writes Hermes settings. Source material goes only to the chosen inference API.
"""
from __future__ import annotations

import ast
import http.client
import json
import logging
import os
from pathlib import Path
import re
import shlex
import shutil
import ssl
import subprocess
import sys
from urllib.parse import urlsplit


class ProviderUnavailable(RuntimeError):
    pass


_CHECK_MESSAGES = {
    "ready": "已找到 Hermes 当前运行环境，模型配置属于支持的调用方式。尚未调用模型接口，未验证密钥有效性、额度或网络；实际整理时才会发送正文。",
    "environment": "暂时无法使用 Hermes 当前运行环境，不能生成摘要。请让助手检查现有 Hermes 安装；不会自动安装或升级 Hermes。",
    "selection": "当前模型选择无法只读确定，或使用外部命令提供服务；此版本暂不能生成摘要。不会改动你的模型设置。",
    "mode": "当前模型配置不属于已接通的调用方式。支持 Chat Completions、Anthropic Messages 和 Hermes 原生 ChatGPT／Codex 订阅；不会改动你的模型设置。",
    "subscription_route": "订阅模型的目标地址不是已接通的 ChatGPT 官方地址；未发送正文或令牌，也不会改用其他模型服务。",
    "configuration": "暂时无法只读核实当前 Hermes 模型配置，不能确认摘要能力。没有调用模型接口，也没有修改设置。",
}

_FAILURE_MESSAGES = {
    "authentication": "Hermes 现有模型登录未配置、已失效或被拒绝；请在 Hermes 中恢复原有登录后重试。正文已保留，未切换其他模型服务。",
    "quota": "当前模型暂时限流或额度不可用；正文已保留，稍后可重试，未切换到额外收费服务。",
    "subscription_route": _CHECK_MESSAGES["subscription_route"],
    "unavailable": "当前 Hermes 模型暂时无法生成摘要；正文已保留供重试，未切换其他模型服务。",
}


def _official_subscription_base(base) -> bool:
    # The endpoint belongs to the installed Hermes native adapter. A custom
    # gateway must never receive a user's subscription bearer by accident.
    if not isinstance(base, str) or re.search(r"[\x00-\x20\x7f\\]", base):
        return False
    try:
        parsed = urlsplit(base)
        return (parsed.scheme == "https" and parsed.hostname == "chatgpt.com"
                and parsed.port in {None, 443} and not parsed.username and not parsed.password
                and not parsed.query and not parsed.fragment
                and parsed.path.rstrip("/") == "/backend-api/codex")
    except ValueError:
        return False


def _failure_reason(error) -> str:
    """Classify only structured host/SDK fields; never expose a remote message."""
    if getattr(error, "status_code", None) in {401, 403} or getattr(error, "relogin_required", False):
        return "authentication"
    if getattr(error, "status_code", None) == 429 or getattr(error, "code", "") in {
        "codex_rate_limited", "codex_quota_exhausted", "rate_limit_exceeded", "insufficient_quota",
    }:
        return "quota"
    return "unavailable"


def _launcher_root() -> Path | None:
    """Read an installed launcher as data; never start Hermes to discover it."""
    launcher = shutil.which("hermes")
    if not launcher:
        return None
    try:
        path = Path(launcher)
        for _ in range(3):
            if path.stat().st_size > 65536:
                return None
            source = path.read_text("utf-8")
            if not source.startswith("#!/bin/sh"):
                break
            words = shlex.split(source, comments=True)
            if not words or words[0] != "exec":
                return None
            if "-c" in words:
                source = words[words.index("-c") + 1]
                break
            # Published PATH shims only forward to one installation launcher.
            if len(words) != 3 or words[2] != "$@" or not Path(words[1]).is_absolute():
                return None
            path = Path(words[1])
        else:
            return None
        tree = ast.parse(source)
        roots = []
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "insert" and isinstance(node.func.value, ast.Attribute)
                    and node.func.value.attr == "path" and isinstance(node.func.value.value, ast.Name)
                    and node.func.value.value.id == "sys" and len(node.args) == 2
                    and isinstance(node.args[0], ast.Constant) and node.args[0].value == 0
                    and isinstance(node.args[1], ast.Constant) and isinstance(node.args[1].value, str)):
                roots.append(Path(node.args[1].value))
        if len(roots) == 1 and roots[0].is_absolute():
            return roots[0]
    except (OSError, UnicodeError, ValueError, SyntaxError, IndexError):
        pass
    return None


def _helper(interpreter: Path, mode: str, root: Path, *, body: bytes = b"", timeout: int = 20) -> dict:
    args = [str(interpreter), "-I", "-B", str(Path(__file__).resolve()), mode, str(root)]
    completed = subprocess.run(args, input=body, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                               timeout=timeout, check=False, cwd=str(root))
    if completed.returncode or len(completed.stdout) > 64000:
        raise ProviderUnavailable()
    data = json.loads(completed.stdout)
    if not isinstance(data, dict):
        raise ProviderUnavailable()
    return data


def _hermes_install(config):
    candidates = []
    if config.get("hermes_root"):
        candidates.append(Path(config["hermes_root"]).expanduser())
    else:
        if discovered := _launcher_root():
            candidates.append(discovered)
        candidates.extend((Path.home() / ".hermes" / "hermes-agent", Path.home() / "hermes-agent"))
    for root in candidates:
        if not (root / "hermes_cli" / "runtime_provider.py").is_file():
            continue
        if (root / "pm" / "environments.py").is_file():
            # PM owns the current selection. A broken record must not silently
            # reactivate an old in-tree venv. This probe never bootstraps PM.
            try:
                selected = _helper(Path(sys.executable), "--select-runtime", root.resolve())
                value = selected.get("python")
                if not isinstance(value, str) or not value or len(value) > 4096:
                    raise ValueError()
                binary = Path(value)
                if not binary.is_absolute() or not binary.is_file() or not os.access(binary, os.X_OK):
                    raise ValueError()
                return root.resolve(), binary.absolute()
            except (OSError, ValueError, subprocess.SubprocessError, ProviderUnavailable):
                raise ProviderUnavailable(_CHECK_MESSAGES["environment"]) from None
        for environment in ("venv", ".venv"):
            for binary in (root / environment / "bin" / "python", root / environment / "Scripts" / "python.exe"):
                if binary.is_file() and os.access(binary, os.X_OK):
                    # Resolving a venv interpreter symlink selects the base Python
                    # and loses Hermes' installed dependencies (sys.prefix).
                    return root.resolve(), binary.absolute()
    raise ProviderUnavailable("未找到可复用的 Hermes 模型环境；已保留原文，未生成摘要。")


def check_provider(*, config: dict | None = None) -> dict:
    """Read-only compatibility check; never resolve credentials or call an API."""
    try:
        root, interpreter = _hermes_install(config or {})
    except Exception:
        return {"available": False, "message": _CHECK_MESSAGES["environment"]}
    try:
        outcome = _helper(interpreter, "--check-provider", root)
        reason = outcome.get("reason")
        if reason not in _CHECK_MESSAGES:
            reason = "configuration"
        return {"available": reason == "ready", "message": _CHECK_MESSAGES[reason]}
    except Exception:
        return {"available": False, "message": _CHECK_MESSAGES["configuration"]}


def complete_json(messages: list[dict], *, config: dict | None = None) -> str:
    """Isolated helper cannot return credentials, tools, runtime config or raw errors."""
    config = config or {}
    root, interpreter = _hermes_install(config)
    body = json.dumps({"messages": messages}, ensure_ascii=False).encode("utf-8")
    if len(body) > 240000:
        raise ProviderUnavailable("待总结内容过长；已保留原文。")
    # This is our fixed adapter script, never a command generated by source/model text.
    args = [str(interpreter), "-I", "-B", str(Path(__file__).resolve()), "--worker", str(root)]
    try:
        result = subprocess.run(args, input=body, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                timeout=70, check=False, cwd=str(root))
        if result.returncode or len(result.stdout) > 64000:
            raise ValueError("worker unavailable")
        outcome = json.loads(result.stdout)
        if isinstance(outcome, dict) and outcome.get("error") in _FAILURE_MESSAGES:
            raise ProviderUnavailable(_FAILURE_MESSAGES[outcome["error"]])
        if not isinstance(outcome, dict) or not isinstance(outcome.get("text"), str) or len(outcome["text"]) > 30000:
            raise ValueError("invalid worker output")
        return outcome["text"]
    except (OSError, ValueError, subprocess.TimeoutExpired):
        raise ProviderUnavailable("当前 Hermes 模型暂时无法安全生成摘要；已保留原文供核对。") from None


def _contains_commands(value):
    # Some providers resolve credentials by running local commands; this adapter
    # deliberately leaves those providers to Hermes itself.
    if isinstance(value, dict):
        if any(k in value and value[k] for k in ("command", "key_cmd", "api_key_cmd", "credential_command")):
            return True
        return any(_contains_commands(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_commands(item) for item in value)
    return False


def _check_configuration() -> str:
    # Import the actual adapter dependency, but never call its credential resolver.
    from hermes_cli.config import load_config_readonly
    from hermes_cli.runtime_provider import resolve_runtime_provider  # noqa: F401
    from hermes_cli.runtime_provider_backends import _is_external_process_provider  # noqa: F401

    settings = load_config_readonly()
    model = settings.get("model") if isinstance(settings, dict) else None
    if not isinstance(model, dict):
        return "selection"
    provider = model.get("provider")
    name = model.get("default") or model.get("model")
    if (not isinstance(provider, str) or not isinstance(name, str) or not name
            or provider in {"", "auto", "moa", "acp", "external", "codex-app-server"}
            or model.get("openai_runtime") == "codex_app_server" or _contains_commands(model)):
        return "selection"
    entries = []
    named = provider.removeprefix("custom:")
    for section in ("providers", "custom_providers"):
        items = settings.get(section) or {}
        if isinstance(items, dict) and isinstance(items.get(named), dict):
            entries.append(items[named])
        if isinstance(items, list):
            entries.extend(item for item in items if isinstance(item, dict) and item.get("name") == named)
    if len(entries) > 1 or any(_contains_commands(entry) for entry in entries):
        return "selection"
    entry = entries[0] if entries else {}
    pdef = None
    if not entry and provider != "custom":
        from hermes_cli.providers import get_provider
        # Older Hermes versions without this offline flag fail closed in preflight.
        pdef = get_provider(provider, allow_network=False)
        if pdef is None or getattr(pdef, "auth_type", "") in {"external_process", "aws_sdk", "vertex"}:
            return "selection"
    mode = model.get("api_mode") or entry.get("api_mode") or entry.get("transport")
    if not mode:
        mode = getattr(pdef, "transport", None) or ("chat_completions" if entry or provider == "custom" else None)
    if provider in {"nous", "nous-portal", "nousresearch"} and not model.get("api_mode"):
        from hermes_cli.providers import nous_api_mode
        mode = nous_api_mode(name)  # Only reads the configured wire preference.
    mode = {"openai_chat": "chat_completions", "openai": "chat_completions",
            "anthropic": "anthropic_messages"}.get(mode, mode) if isinstance(mode, str) else None
    # Hosted endpoints can require a different protocol than a stale saved mode.
    base = model.get("base_url") or entry.get("base_url") or entry.get("url") or entry.get("api")
    if not base and pdef is not None:
        env_name = getattr(pdef, "base_url_env_var", "")
        base = (os.environ.get(env_name) if env_name else None) or getattr(pdef, "base_url", "")
    if base:
        if not isinstance(base, str) or re.search(r"[\x00-\x20\x7f\\]", base):
            return "configuration"
        parsed = urlsplit(base)
        if (parsed.username or parsed.password or parsed.query or parsed.fragment or not parsed.hostname
                or (parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}))):
            return "configuration"
        from hermes_cli.providers import host_mandated_api_mode
        mode = host_mandated_api_mode(base) or mode
    if mode == "codex_responses" and getattr(pdef, "id", provider) == "openai-codex":
        if not _official_subscription_base(base):
            return "subscription_route"
        # Import the real no-tools adapter, not credentials or an agent. This
        # also checks that the selected Hermes interpreter has its SDK installed.
        from agent.auxiliary_client import CodexAuxiliaryClient  # noqa: F401
        from agent.codex_headers import codex_cloudflare_headers  # noqa: F401
        import httpx  # noqa: F401
        from openai import OpenAI  # noqa: F401
        return "ready"
    if mode not in {"chat_completions", "anthropic_messages"}:
        return "mode"
    if not base and (entry or provider == "custom"):
        return "configuration"
    return "ready"


def _read_only_probe() -> None:
    """Fence accidental upstream side effects during metadata/configuration checks."""
    def audit(event, args):
        if (event in {"subprocess.Popen", "os.system", "os.posix_spawn", "os.exec",
                      "socket.connect", "socket.bind", "socket.getaddrinfo", "socket.gethostbyname",
                      "os.mkdir", "os.remove", "os.rmdir", "os.rename", "os.chmod", "os.chown",
                      "os.link", "os.symlink", "os.truncate"}
                or (event == "open" and len(args) >= 3 and isinstance(args[2], int)
                    and args[2] & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND))):
            raise PermissionError("read-only provider probe")
    sys.addaudithook(audit)


def _suppress_probe_config_writes() -> None:
    # Hermes' "readonly" loader means shared return value, not zero filesystem
    # writes: its cold path initializes home and takes a config backup. Disable
    # only those housekeeping hooks in this short-lived helper; the probe fence
    # still rejects any other unexpected mutation, command, or network call.
    from hermes_cli import config
    if hasattr(config, "ensure_hermes_home"):
        config.ensure_hermes_home = lambda: None
    try:
        from hermes_cli import config_backups
    except ImportError:
        return  # Earlier Hermes and test doubles have no backup module.
    config_backups.backup_config = lambda *args, **kwargs: None


def _probe_worker(root: str, mode: str) -> int:
    original_stdout = os.dup(1)
    try:
        with open(os.devnull, "w") as sink:
            os.dup2(sink.fileno(), 1)
            os.dup2(sink.fileno(), 2)
            logging.disable(logging.CRITICAL)
            sys.dont_write_bytecode = True
            sys.path.insert(0, root)
            _read_only_probe()
            if mode == "--select-runtime":
                from pm.environments import project_python
                outcome = {"python": str(project_python(Path(root)).absolute())}
            else:
                _suppress_probe_config_writes()
                outcome = {"reason": _check_configuration()}
        os.write(original_stdout, json.dumps(outcome).encode())
        return 0
    except Exception:
        return 1
    finally:
        os.close(original_stdout)


def _runtime():
    from hermes_cli.config import load_config_readonly
    from hermes_cli.runtime_provider import resolve_runtime_provider
    from hermes_cli.runtime_provider_backends import _is_external_process_provider

    settings = load_config_readonly()
    model_settings = settings.get("model")
    if not isinstance(model_settings, dict):
        raise ProviderUnavailable()
    provider = model_settings.get("provider")
    model = model_settings.get("default") or model_settings.get("model")
    if not isinstance(provider, str) or provider in {"", "auto", "moa", "acp", "external", "codex-app-server"} or not isinstance(model, str) or not model:
        raise ProviderUnavailable()
    # Do not evaluate providers that use a separate agent or configured shell hook.
    if model_settings.get("openai_runtime") == "codex_app_server" or _is_external_process_provider(provider) or _contains_commands(model_settings):
        raise ProviderUnavailable()
    named = provider.removeprefix("custom:")
    for section in ("providers", "custom_providers"):
        entries = settings.get(section) or {}
        if isinstance(entries, dict) and _contains_commands(entries.get(named, {})):
            raise ProviderUnavailable()
        if isinstance(entries, list) and any(isinstance(item, dict) and item.get("name") == named and _contains_commands(item) for item in entries):
            raise ProviderUnavailable()
    runtime = resolve_runtime_provider(requested=provider, target_model=model)
    subscription = (runtime.get("provider") == "openai-codex"
                    and runtime.get("api_mode") == "codex_responses")
    if (runtime.get("api_mode") not in {"chat_completions", "anthropic_messages"} and not subscription) or runtime.get("command"):
        raise ProviderUnavailable()
    if subscription and not _official_subscription_base(runtime.get("base_url")):
        raise ProviderUnavailable(_FAILURE_MESSAGES["subscription_route"])
    if not isinstance(runtime.get("api_key"), str):
        raise ProviderUnavailable()
    runtime["model"] = model
    return runtime


def _request_subscription(runtime, messages):
    """Use Hermes' native Responses adapter; do not recreate its wire protocol."""
    if runtime.get("provider") != "openai-codex" or not _official_subscription_base(runtime.get("base_url")):
        raise ProviderUnavailable(_FAILURE_MESSAGES["subscription_route"])
    import httpx
    from openai import OpenAI
    from agent.auxiliary_client import CodexAuxiliaryClient
    from agent.codex_headers import codex_cloudflare_headers

    class BoundedStream(httpx.SyncByteStream):
        def __init__(self, stream):
            self.stream = stream

        def __iter__(self):
            received = 0
            for chunk in self.stream:
                received += len(chunk)
                if received > 2_000_000:
                    raise ProviderUnavailable()
                yield chunk

        def close(self):
            self.stream.close()

    def limit_response(response):
        if response.headers.get("Content-Encoding", "identity").lower() != "identity":
            response.close()
            raise ProviderUnavailable()
        response.stream = BoundedStream(response.stream)

    # Same official route + host-provided account headers, with no implicit
    # fallback, network proxy, redirects, SDK retry, or model tool execution.
    with httpx.Client(trust_env=False, follow_redirects=False, timeout=55,
                      headers={"Accept-Encoding": "identity"},
                      event_hooks={"response": [limit_response]}) as transport:
        with OpenAI(api_key=runtime["api_key"], base_url=runtime["base_url"],
                    http_client=transport, max_retries=0, timeout=55,
                    default_headers=codex_cloudflare_headers(runtime["api_key"], base_url=runtime["base_url"])) as native:
            client = CodexAuxiliaryClient(native, runtime["model"])
            result = client.chat.completions.create(model=runtime["model"], messages=messages, timeout=55)
            message = result.choices[0].message
            text = message.content
            if (getattr(message, "tool_calls", None) or getattr(message, "function_call", None)
                    or not isinstance(text, str) or not text or len(text) > 30000):
                raise ProviderUnavailable()
            return text


def _request(runtime, messages):
    """Credentialed HTTP request. Destination comes exclusively from Hermes config.

    We do not follow redirects or use proxy environment variables. Plain HTTP is
    allowed solely for an explicitly configured loopback model server.
    """
    if runtime.get("api_mode") == "codex_responses":
        return _request_subscription(runtime, messages)
    base = runtime.get("base_url", "")
    if not isinstance(base, str) or re.search(r"[\x00-\x20\x7f\\]", base):
        raise ProviderUnavailable()
    parsed = urlsplit(base)
    if parsed.username or parsed.password or parsed.query or parsed.fragment or not parsed.hostname:
        raise ProviderUnavailable()
    loopback = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    if parsed.scheme != "https" and not (parsed.scheme == "http" and loopback):
        raise ProviderUnavailable()
    token = runtime["api_key"]
    headers = {"Content-Type": "application/json", "Accept": "application/json", "Accept-Encoding": "identity"}
    mode = runtime["api_mode"]
    if mode == "anthropic_messages":
        base_path = parsed.path.rstrip("/")
        endpoint = base_path + ("/messages" if base_path.endswith("/v1") else "/v1/messages")
        system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
        payload = {"model": runtime["model"], "system": system, "messages": [m for m in messages if m["role"] != "system"], "max_tokens": 3000}
        headers.update({"x-api-key": token, "anthropic-version": "2023-06-01"})
    else:
        endpoint = parsed.path.rstrip("/") + "/chat/completions"
        payload = {"model": runtime["model"], "messages": messages, "max_completion_tokens": 3000}
        # Older compatible providers implement max_tokens, reasoning OpenAI models
        # use max_completion_tokens. No unbounded request or retry is attempted.
        if not re.match(r"^(?:o[1-9]|gpt-5|gpt-6)(?:[.-]|$)", runtime["model"]):
            payload["max_tokens"] = payload.pop("max_completion_tokens")
        if token:
            headers["Authorization"] = "Bearer " + token
    connection_class = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
    kwargs = {"timeout": 55}
    if parsed.scheme == "https":
        kwargs["context"] = ssl.create_default_context()
    connection = connection_class(parsed.hostname, parsed.port, **kwargs)
    try:
        connection.request("POST", endpoint, body=json.dumps(payload, ensure_ascii=False).encode(), headers=headers)
        response = connection.getresponse()
        if response.status != 200 or (response.getheader("Content-Encoding") or "identity") != "identity":
            raise ProviderUnavailable()
        raw = response.read(240001)
        if len(raw) > 240000:
            raise ProviderUnavailable()
        data = json.loads(raw)
        if mode == "anthropic_messages":
            blocks = data.get("content", [])
            if any(item.get("type") == "tool_use" for item in blocks):
                raise ProviderUnavailable()
            text = "".join(item.get("text", "") for item in blocks if item.get("type") == "text")
        else:
            message = data["choices"][0]["message"]
            if message.get("tool_calls") or message.get("function_call"):
                raise ProviderUnavailable()
            text = message.get("content")
        if not isinstance(text, str) or len(text) > 30000:
            raise ProviderUnavailable()
        return text
    finally:
        connection.close()


def _worker(root):
    body = sys.stdin.buffer.read(240001)
    if len(body) > 240000:
        return 1
    # Silence libraries at the descriptor level: upstream error/log output can
    # include provider endpoints or tokens and must never reach the caller.
    original_stdout = os.dup(1)
    try:
        with open(os.devnull, "w") as sink:
            os.dup2(sink.fileno(), 1)
            os.dup2(sink.fileno(), 2)
            logging.disable(logging.CRITICAL)
            sys.path.insert(0, root)
            _suppress_probe_config_writes()
            request = json.loads(body)
            messages = request["messages"]
            if not isinstance(messages, list) or len(messages) != 2 or any(set(m) != {"role", "content"} for m in messages):
                return 1
            text = _request(_runtime(), messages)
        result = json.dumps({"text": text}, ensure_ascii=False).encode()
        os.write(original_stdout, result)
        return 0
    except Exception as error:
        # Fixed classifications only. Credentials and remote exception strings
        # cannot cross the worker boundary, including native OAuth failures.
        reason = ("subscription_route" if isinstance(error, ProviderUnavailable)
                  and str(error) == _FAILURE_MESSAGES["subscription_route"] else _failure_reason(error))
        os.write(original_stdout, json.dumps({"error": reason}).encode())
        return 0
    finally:
        os.close(original_stdout)


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] not in {"--worker", "--select-runtime", "--check-provider"}:
        raise SystemExit(2)
    if sys.argv[1] == "--worker":
        raise SystemExit(_worker(sys.argv[2]))
    raise SystemExit(_probe_worker(sys.argv[2], sys.argv[1]))
