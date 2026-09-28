"""Optional ASR install/upgrade contracts; empty locks and offline subprocesses."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import zipfile

import pytest

from test_install_recovery import release, offline_runner, active_runtime
from test_installer import installer, builder


@pytest.fixture
def asr_release(release):
    source, skills = release
    (source / "requirements-asr.lock").write_text("# offline ASR fixture, no packages\n")
    builder.build(source)
    return source, skills


def install(release, **kwargs):
    return installer.install(*release, Path(sys.executable), runner=kwargs.pop("runner", offline_runner), **kwargs)


def marker(skills):
    return skills / installer.SKILL_NAME / installer.MARKER


def test_asr_preview_discloses_cost_without_installing(asr_release):
    calls = []
    result = install(asr_release, with_local_asr=True, dry_run=True,
                     runner=lambda *a, **k: calls.append(a))
    assert result["local_asr"] is True and "60–85 MB" in result["message"]
    assert result["required_python"] == ">=3.12"
    assert "不下载识别模型" in result["message"] and "CPU" in result["message"]
    assert calls == [] and not asr_release[1].exists()
    assert install(asr_release, dry_run=True)["local_asr"] is False
    assert install(asr_release, dry_run=True)["required_python"] == ">=3.11"


def test_asr_rejects_python_311_before_changing_core_install(asr_release):
    install(asr_release)
    old_marker = marker(asr_release[1]).read_bytes()
    old_runtime = active_runtime(asr_release[1])
    calls = []
    def old_python(args, **kwargs):
        calls.append(args)
        assert args[1] == "-c" and "(3, 12)" in args[2]
        raise subprocess.CalledProcessError(1, args)
    with pytest.raises(installer.InstallError, match="Python 3.12"):
        install(asr_release, with_local_asr=True, runner=old_python)
    assert len(calls) == 1
    assert marker(asr_release[1]).read_bytes() == old_marker
    assert old_runtime.is_dir() and list(old_runtime.parent.iterdir()) == [old_runtime]
    assert not (asr_release[1] / installer.TRANSACTION).exists()


def test_opt_in_same_release_installs_both_locks_and_keeps_core_runtime(asr_release):
    core = install(asr_release)
    old_runtime = active_runtime(asr_release[1])
    calls = []
    def runner(args, **kwargs):
        calls.append(args)
        return offline_runner(args, **kwargs)
    enabled = install(asr_release, with_local_asr=True, runner=runner)
    receipt = json.loads(marker(asr_release[1]).read_text())
    manifest_hash = hashlib.sha256((asr_release[0] / installer.MANIFEST).read_bytes()).hexdigest()
    assert core["release"] == manifest_hash
    assert receipt["manifest_sha256"] == enabled["manifest_sha256"] == manifest_hash
    assert receipt["local_asr"] is enabled["local_asr"] is True
    assert enabled["release"] != core["release"] and not enabled["already_installed"]
    assert old_runtime.is_dir() and receipt["previous_release"] == core["release"]
    pip_calls = [args for args in calls if "pip" in args]
    assert [Path(args[-1]).name for args in pip_calls[:2]] == ["requirements.lock", "requirements-asr.lock"]
    for args in pip_calls[:2]:
        assert all(flag in args for flag in ("--isolated", "--require-hashes", "--only-binary=:all:", "https://pypi.org/simple"))
    assert pip_calls[2][1:4] == ["-m", "pip", "check"]
    count = len(calls)
    repeated = install(asr_release, runner=runner)
    assert repeated["already_installed"] and repeated["local_asr"]
    assert len(calls) == count


def test_upgrade_inherits_asr_and_legacy_receipts_default_to_core(asr_release):
    first = install(asr_release, with_local_asr=True)
    (asr_release[0] / "VERSION").write_text("2.0.3\n")
    builder.build(asr_release[0])
    second = install(asr_release)
    assert second["local_asr"] and second["release"] != first["release"]
    receipt_path = marker(asr_release[1])
    state = json.loads(receipt_path.read_text())
    state.pop("local_asr")
    receipt_path.write_text(json.dumps(state))
    assert install(asr_release, dry_run=True)["local_asr"] is False


def test_missing_or_tampered_asr_lock_does_not_run_installer(release):
    calls = []
    with pytest.raises(installer.InstallError, match="清单"):
        install(release, with_local_asr=True, runner=lambda *a, **k: calls.append(a))
    assert calls == []
    source, _ = release
    (source / "requirements-asr.lock").write_text("# original\n")
    builder.build(source)
    (source / "requirements-asr.lock").write_text("untrusted dependency\n")
    with pytest.raises(installer.InstallError, match="校验失败"):
        install(release, with_local_asr=True, runner=lambda *a, **k: calls.append(a))
    assert calls == []


@pytest.mark.parametrize("failure", ["asr_install", "dependency_check"])
def test_failed_asr_enable_keeps_old_installation(asr_release, failure):
    install(asr_release)
    old_marker = marker(asr_release[1]).read_bytes()
    old_runtime = active_runtime(asr_release[1])
    def fail(args, **kwargs):
        if ((failure == "asr_install" and Path(args[-1]).name == "requirements-asr.lock")
                or (failure == "dependency_check" and args[1:4] == ["-m", "pip", "check"])):
            raise subprocess.CalledProcessError(1, args)
        return offline_runner(args, **kwargs)
    with pytest.raises(subprocess.CalledProcessError):
        install(asr_release, with_local_asr=True, runner=fail)
    assert marker(asr_release[1]).read_bytes() == old_marker
    assert old_runtime.is_dir() and list(old_runtime.parent.iterdir()) == [old_runtime]
    assert not (asr_release[1] / installer.TRANSACTION).exists()


def test_hard_interrupt_during_asr_install_recovers_existing_runtime(asr_release):
    install(asr_release)
    old_runtime = active_runtime(asr_release[1])
    pid = os.fork()
    if pid == 0:
        def interrupted(args, **kwargs):
            if Path(args[-1]).name == "requirements-asr.lock":
                os._exit(137)
            return offline_runner(args, **kwargs)
        try:
            install(asr_release, with_local_asr=True, runner=interrupted)
        except BaseException:
            os._exit(99)
        os._exit(98)
    _, status = os.waitpid(pid, 0)
    assert os.waitstatus_to_exitcode(status) == 137
    assert (asr_release[1] / installer.TRANSACTION).exists() and old_runtime.is_dir()
    result = install(asr_release, with_local_asr=True)
    assert result["local_asr"] and active_runtime(asr_release[1]) != old_runtime
    assert old_runtime.is_dir() and not (asr_release[1] / installer.TRANSACTION).exists()


def test_unknown_or_user_modified_skill_is_still_protected(asr_release):
    install(asr_release)
    path = asr_release[1] / installer.SKILL_NAME / "SKILL.md"
    path.write_text("user changes\n")
    with pytest.raises(installer.InstallError, match="修改"):
        install(asr_release, with_local_asr=True)
    assert path.read_text() == "user changes\n"


def test_asr_lock_is_in_manifest_and_archive(asr_release, tmp_path):
    archive = tmp_path / "asr-candidate.zip"
    builder.build(asr_release[0], archive)
    manifest = json.loads((asr_release[0] / installer.MANIFEST).read_text())
    assert "requirements-asr.lock" in manifest["files"]
    with zipfile.ZipFile(archive) as contents:
        matching = [name for name in contents.namelist() if name.endswith("/requirements-asr.lock")]
        assert len(matching) == 1
        assert hashlib.sha256(contents.read(matching[0])).hexdigest() == manifest["files"]["requirements-asr.lock"]
