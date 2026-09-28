"""Only fake Hermes source/configuration: never inspect the user's model setup."""
import json
from pathlib import Path
import shlex
import sys

import pytest

from video_kb import providers


def fake_hermes(tmp_path, *, settings=None, config_code=None, pm_code=None):
    root = (tmp_path / "fake-hermes").resolve()
    package = root / "hermes_cli"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / "config.py").write_text(config_code or (
        "def load_config_readonly():\n    return " + repr(settings or {
            "model": {"provider": "custom", "default": "fake-model", "api_mode": "chat_completions",
                      "base_url": "https://private-endpoint.invalid/v1", "api_key": "DO_NOT_RETURN_SECRET"}
        }) + "\n"))
    (package / "runtime_provider.py").write_text(
        "def resolve_runtime_provider(*a, **kw):\n    raise AssertionError('credential resolver must not run')\n")
    (package / "runtime_provider_backends.py").write_text(
        "def _is_external_process_provider(*a, **kw):\n    raise AssertionError('dynamic provider lookup must not run')\n")
    (package / "providers.py").write_text(
        "from types import SimpleNamespace\n"
        "def get_provider(name, *, allow_network):\n"
        "    assert allow_network is False\n"
        "    return SimpleNamespace(transport='anthropic_messages', auth_type='api_key', base_url='', base_url_env_var='')\n"
        "def host_mandated_api_mode(url):\n    return 'codex_responses' if 'api.openai.com' in url else None\n")
    legacy = root / "venv" / "bin" / "python"
    legacy.parent.mkdir(parents=True)
    legacy.symlink_to(sys.executable)
    if pm_code is not None:
        (root / "pm").mkdir()
        (root / "pm" / "__init__.py").write_text("")
        (root / "pm" / "environments.py").write_text(pm_code)
    return root, legacy


def check(root):
    return providers.check_provider(config={"hermes_root": str(root)})


def test_legacy_symlink_keeps_virtual_environment_path(tmp_path):
    root, binary = fake_hermes(tmp_path)
    actual_root, selected = providers._hermes_install({"hermes_root": str(root)})
    assert actual_root == root
    assert selected == binary.absolute()
    assert selected != binary.resolve()


def test_pm_selects_current_environment_instead_of_stale_venv(tmp_path):
    selected = (tmp_path / "selected-generation" / "bin" / "python").resolve()
    selected.parent.mkdir(parents=True)
    selected.symlink_to(sys.executable)
    root, legacy = fake_hermes(tmp_path, pm_code=(
        "from pathlib import Path\ndef project_python(root):\n    return Path(" + repr(str(selected)) + ")\n"))
    assert providers._hermes_install({"hermes_root": str(root)}) == (root, selected)
    assert legacy.exists()
    assert check(root)["available"] is True


@pytest.mark.parametrize("pm_code", [
    "def project_python(root):\n    raise RuntimeError('broken PM selection')\n",
    "from pathlib import Path\ndef project_python(root):\n    return root / 'missing' / 'bin' / 'python'\n",
])
def test_broken_pm_selection_never_falls_back(tmp_path, pm_code):
    root, legacy = fake_hermes(tmp_path, pm_code=pm_code)
    with pytest.raises(providers.ProviderUnavailable):
        providers._hermes_install({"hermes_root": str(root)})
    assert legacy.exists()
    assert check(root) == {"available": False, "message": providers._CHECK_MESSAGES["environment"]}


def test_preflight_only_returns_fixed_message_without_resolving_credentials(tmp_path):
    root, _ = fake_hermes(tmp_path)
    before = sorted(str(p.relative_to(root)) for p in root.rglob("*"))
    result = check(root)
    assert result == {"available": True, "message": providers._CHECK_MESSAGES["ready"]}
    assert "private-endpoint" not in json.dumps(result)
    assert "DO_NOT_RETURN_SECRET" not in json.dumps(result)
    assert before == sorted(str(p.relative_to(root)) for p in root.rglob("*"))


@pytest.mark.parametrize("model, reason", [
    ({"provider": "auto", "default": "fake"}, "selection"),
    ({"provider": "custom", "default": "fake", "api_mode": "codex_responses"}, "mode"),
    ({"provider": "custom", "default": "fake", "api_mode": "chat_completions", "base_url": "https://api.openai.com/v1"}, "mode"),
    ({"provider": "custom", "default": "fake", "api_mode": "chat_completions", "key_cmd": "do-not-execute"}, "selection"),
    ({"provider": "custom", "default": "fake", "base_url": "http://remote.invalid"}, "configuration"),
])
def test_preflight_reports_unsupported_configurations(tmp_path, model, reason):
    root, _ = fake_hermes(tmp_path, settings={"model": model})
    assert check(root) == {"available": False, "message": providers._CHECK_MESSAGES[reason]}


def test_anthropic_provider_uses_only_offline_registry_metadata(tmp_path):
    root, _ = fake_hermes(tmp_path, settings={"model": {"provider": "anthropic", "default": "fake"}})
    assert check(root)["available"] is True


@pytest.mark.parametrize("code", [
    "import socket\nsocket.create_connection(('127.0.0.1', 9))\n",
    "import subprocess\nsubprocess.run(['must-never-execute'])\n",
    "from pathlib import Path\nPath(__file__).with_name('unexpected-write').write_text('bad')\n",
    "print('DO_NOT_RETURN_SECRET')\nraise RuntimeError('private-endpoint.invalid')\n",
])
def test_preflight_fences_network_process_writes_and_redacts_errors(tmp_path, code):
    root, _ = fake_hermes(tmp_path, config_code=code)
    result = check(root)
    assert result == {"available": False, "message": providers._CHECK_MESSAGES["configuration"]}
    assert not (root / "hermes_cli" / "unexpected-write").exists()


def test_launcher_discovery_parses_but_never_executes_code(tmp_path, monkeypatch):
    root = tmp_path / "custom-root"
    launcher = tmp_path / "hermes"
    launcher.write_text("import sys\nsys.path.insert(0, " + repr(str(root)) + ")\nraise AssertionError('never execute')\n")
    monkeypatch.setattr(providers.shutil, "which", lambda _: str(launcher))
    assert providers._launcher_root() == root


def test_launcher_discovery_follows_only_literal_forwarding_shim(tmp_path, monkeypatch):
    root = tmp_path / "custom root"
    inner = tmp_path / "installed launcher"
    code = "import sys; sys.path.insert(0, " + repr(str(root)) + "); raise RuntimeError('never execute')"
    inner.write_text("#!/bin/sh\nexec " + shlex.join([sys.executable, "-I", "-c", code]) + ' "$@"\n')
    launcher = tmp_path / "hermes"
    launcher.write_text("#!/bin/sh\nexec " + shlex.quote(str(inner)) + ' "$@"\n')
    monkeypatch.setattr(providers.shutil, "which", lambda _: str(launcher))
    assert providers._launcher_root() == root


def test_explicit_missing_test_root_does_not_discover_user_install(tmp_path, monkeypatch):
    monkeypatch.setattr(providers, "_launcher_root", lambda: pytest.fail("must not discover another installation"))
    assert check(tmp_path / "missing")["available"] is False
