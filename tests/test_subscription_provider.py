"""Subscription bridge tests use synthetic settings and tokens, never user auth."""
import json
from types import SimpleNamespace
import sys

import pytest

from video_kb import providers
from test_provider_runtime import fake_hermes


OFFICIAL = "https://chatgpt.com/backend-api/codex"


def subscription_hermes(tmp_path, *, base=OFFICIAL, failure=None):
    root, binary = fake_hermes(tmp_path, settings={"model": {
        "provider": "openai-codex", "default": "synthetic-model", "api_mode": "codex_responses",
        "base_url": base,
    }})
    package = root / "hermes_cli"
    (package / "providers.py").write_text(
        "from types import SimpleNamespace\n"
        "def get_provider(name, *, allow_network):\n"
        "    assert allow_network is False\n"
        "    return SimpleNamespace(id='openai-codex', transport='codex_responses', auth_type='oauth_external', base_url='', base_url_env_var='')\n"
        "def host_mandated_api_mode(base):\n    return 'codex_responses'\n")
    (package / "runtime_provider_backends.py").write_text(
        "def _is_external_process_provider(provider):\n    return False\n")
    runtime = {"provider": "openai-codex", "api_mode": "codex_responses", "api_key": "SYNTHETIC_SECRET", "base_url": base}
    (package / "runtime_provider.py").write_text(
        "def resolve_runtime_provider(*, requested, target_model):\n"
        "    assert requested == 'openai-codex'\n"
        + ("    return " + repr(runtime) + "\n" if failure is None else
           "    error = RuntimeError('DO_NOT_RETURN_SECRET_OR_REMOTE_ERROR')\n"
           "    error.relogin_required = " + repr(failure == "auth") + "\n"
           "    error.status_code = " + repr(429 if failure == "quota" else None) + "\n"
           "    raise error\n"))
    (root / "agent").mkdir()
    (root / "agent" / "__init__.py").write_text("")
    (root / "agent" / "codex_headers.py").write_text(
        "def codex_cloudflare_headers(token, *, base_url):\n"
        "    assert token == 'SYNTHETIC_SECRET'\n"
        "    return {'originator':'fake-hermes'}\n")
    (root / "agent" / "auxiliary_client.py").write_text(
        "from types import SimpleNamespace\n"
        "class CodexAuxiliaryClient:\n"
        "    def __init__(self, native, model):\n"
        "        self.chat = SimpleNamespace(completions=self)\n"
        "    def create(self, **kwargs):\n"
        "        assert set(kwargs) == {'model','messages','timeout'}\n"
        "        assert kwargs['messages'][0]['role'] == 'system'\n"
        "        assert kwargs['model'] == 'synthetic-model'\n"
        "        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='native-adapter-result',tool_calls=None))])\n")
    (root / "openai.py").write_text(
        "class OpenAI:\n"
        "    def __init__(self, **kwargs):\n"
        "        assert kwargs['max_retries'] == 0\n"
        "        assert kwargs['base_url'] == 'https://chatgpt.com/backend-api/codex'\n"
        "    def __enter__(self): return self\n"
        "    def __exit__(self, *a): pass\n")
    (root / "httpx.py").write_text(
        "class SyncByteStream: pass\n"
        "class Client:\n"
        "    def __init__(self, **kwargs):\n"
        "        assert kwargs['trust_env'] is False\n"
        "        assert kwargs['follow_redirects'] is False\n"
        "        assert len(kwargs['event_hooks']['response']) == 1\n"
        "    def __enter__(self): return self\n"
        "    def __exit__(self, *a): pass\n")
    return root, binary


def test_subscription_preflight_and_isolated_request_are_both_connected(tmp_path):
    root, _ = subscription_hermes(tmp_path)
    config = {"hermes_root": str(root)}
    assert providers.check_provider(config=config)["available"]
    text = providers.complete_json([
        {"role": "system", "content": "Summarize only; no tools."},
        {"role": "user", "content": "Untrusted source text."},
    ], config=config)
    assert text == "native-adapter-result"
    assert "SYNTHETIC_SECRET" not in text


@pytest.mark.parametrize("base", [
    "https://chatgpt.com.evil.invalid/backend-api/codex", "http://chatgpt.com/backend-api/codex",
    "https://chatgpt.com:444/backend-api/codex", "https://chatgpt.com/backend-api/codex/../other",
    "https://token@chatgpt.com/backend-api/codex", "https://chatgpt.com/backend-api/codex?target=evil",
    "https://api.openai.com/v1", "http://127.0.0.1/v1",
])
def test_subscription_cannot_forward_bearer_to_other_routes(tmp_path, base):
    root, _ = subscription_hermes(tmp_path, base=base)
    assert not providers.check_provider(config={"hermes_root": str(root)})["available"]
    with pytest.raises(providers.ProviderUnavailable):
        providers._request_subscription({"provider": "openai-codex", "base_url": base}, [])


@pytest.mark.parametrize("failure,reason", [("auth", "authentication"), ("quota", "quota"), ("other", "unavailable")])
def test_subscription_errors_are_fixed_and_do_not_fall_back(tmp_path, failure, reason):
    root, _ = subscription_hermes(tmp_path, failure=failure)
    with pytest.raises(providers.ProviderUnavailable) as error:
        providers.complete_json([
            {"role": "system", "content": "text"}, {"role": "user", "content": "text"},
        ], config={"hermes_root": str(root)})
    assert str(error.value) == providers._FAILURE_MESSAGES[reason]
    assert "SECRET" not in str(error.value)


def test_subscription_tool_response_is_rejected_and_transport_is_closed(monkeypatch):
    closed = []
    class Client:
        def __init__(self, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): closed.append(True)
    class NativeAdapter:
        def __init__(self, *args): self.chat = SimpleNamespace(completions=self)
        def create(self, **kwargs):
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
                content='pretend success', tool_calls=[{"function": "execute_shell"}]))])
    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=Client))
    monkeypatch.setitem(sys.modules, "httpx", SimpleNamespace(Client=Client, SyncByteStream=object))
    monkeypatch.setitem(sys.modules, "agent.auxiliary_client", SimpleNamespace(CodexAuxiliaryClient=NativeAdapter))
    monkeypatch.setitem(sys.modules, "agent.codex_headers", SimpleNamespace(codex_cloudflare_headers=lambda *a, **k: {}))
    with pytest.raises(providers.ProviderUnavailable):
        providers._request_subscription({"provider":"openai-codex", "model":"fake", "base_url":OFFICIAL, "api_key":"fake"}, [])
    assert len(closed) == 2


def test_subscription_refuses_encoded_response_before_decoding(monkeypatch):
    closed = []
    class Client:
        def __init__(self, **kwargs):
            assert kwargs["headers"]["Accept-Encoding"] == "identity"
            response = SimpleNamespace(headers={"Content-Encoding": "gzip"}, close=lambda: closed.append(True))
            kwargs["event_hooks"]["response"][0](response)
    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=object))
    monkeypatch.setitem(sys.modules, "httpx", SimpleNamespace(Client=Client, SyncByteStream=object))
    monkeypatch.setitem(sys.modules, "agent.auxiliary_client", SimpleNamespace(CodexAuxiliaryClient=object))
    monkeypatch.setitem(sys.modules, "agent.codex_headers", SimpleNamespace(codex_cloudflare_headers=lambda *a, **k: {}))
    with pytest.raises(providers.ProviderUnavailable):
        providers._request_subscription({"provider":"openai-codex", "model":"fake", "base_url":OFFICIAL, "api_key":"fake"}, [])
    assert closed == [True]
