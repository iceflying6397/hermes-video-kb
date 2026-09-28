"""Real process interruption/concurrency, confined to temporary fixture installs."""
import json
import os
from pathlib import Path
import subprocess
import sys
import threading

import pytest

import test_installer as fixtures

installer = fixtures.installer
builder = fixtures.builder


@pytest.fixture
def release(tmp_path):
    source, skills = tmp_path / "source", tmp_path / "skills"
    files = {
        "VERSION": "2.0.0\n",
        "requirements.lock": "# offline fixture\n",
        "scripts/video_kb.py": "print('fixture')\n",
        "src/video_kb/__init__.py": "",
        "skills/feishu-video-to-notion/SKILL.md": "old skill\n",
        "skills/feishu-video-to-notion/references/help.md": "old help\n",
    }
    for name, content in files.items():
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    builder.build(source)
    return source, skills


def offline_runner(args, **kwargs):
    if args[1:3] == ["-m", "venv"]:
        path = Path(args[3]) / "bin" / "python"
        path.parent.mkdir(parents=True)
        path.write_text("fixture")
    return subprocess.CompletedProcess(args, 0)


def install(release, **kwargs):
    return installer.install(*release, Path(sys.executable), runner=kwargs.pop("runner", offline_runner), **kwargs)


def next_release(release):
    source, _ = release
    (source / "skills/feishu-video-to-notion/SKILL.md").write_text("new skill\n")
    (source / "skills/feishu-video-to-notion/references/help.md").write_text("new help\n")
    builder.build(source)


def active_runtime(skills):
    target = skills / installer.SKILL_NAME
    marker = json.loads((target / installer.MARKER).read_text())
    return target / "releases" / marker["release"]


def crash_after_write(release, filename):
    pid = os.fork()
    if pid == 0:
        original = installer._atomic_write
        def interrupted(path, content, **kwargs):
            original(path, content, **kwargs)
            if path.name == filename:
                os._exit(137)  # Cannot be caught by the installer's rollback.
        installer._atomic_write = interrupted
        try:
            install(release)
        except BaseException:
            os._exit(99)
        os._exit(98)
    _, status = os.waitpid(pid, 0)
    assert os.waitstatus_to_exitcode(status) == 137


def test_concurrent_upgrade_cannot_remove_successful_runtime(release):
    install(release)
    next_release(release)
    entered, finish = threading.Event(), threading.Event()
    results = []
    def slow_runner(args, **kwargs):
        result = offline_runner(args, **kwargs)
        if args[-1] == "--help":
            entered.set()
            assert finish.wait(10)
        return result
    def first_install():
        try:
            results.append(install(release, runner=slow_runner))
        except BaseException as exc:
            results.append(exc)
    worker = threading.Thread(target=first_install)
    worker.start()
    try:
        assert entered.wait(10)
        with pytest.raises(installer.InstallError, match="另一个安装"):
            install(release)
    finally:
        finish.set()
        worker.join(10)
    assert not worker.is_alive()
    assert len(results) == 1 and isinstance(results[0], dict)
    assert active_runtime(release[1]).is_dir()
    assert install(release)["already_installed"]


@pytest.mark.parametrize("filename", ["SKILL.md", "help.md", installer.MARKER])
def test_hard_interrupted_upgrade_recovers_on_next_install(release, filename):
    install(release)
    next_release(release)
    crash_after_write(release, filename)
    journal = release[1] / installer.TRANSACTION
    assert journal.exists()
    before = journal.read_bytes()
    with pytest.raises(installer.InstallError, match="预览没有改动"):
        install(release, dry_run=True)
    assert journal.read_bytes() == before
    install(release)
    assert not journal.exists()
    assert active_runtime(release[1]).is_dir()
    target = release[1] / installer.SKILL_NAME
    assert (target / "SKILL.md").read_text() == "new skill\n"
    assert (target / "references/help.md").read_text() == "new help\n"
    assert install(release)["already_installed"]


def test_hard_interrupted_fresh_install_is_recoverable(release):
    crash_after_write(release, "SKILL.md")
    install(release)
    assert active_runtime(release[1]).is_dir()
    assert not (release[1] / installer.TRANSACTION).exists()


def test_recovery_preserves_real_user_edits(release):
    install(release)
    next_release(release)
    crash_after_write(release, "SKILL.md")
    target = release[1] / installer.SKILL_NAME
    (target / "SKILL.md").write_text("user edits after interruption\n")
    with pytest.raises(installer.InstallError, match="文件被修改"):
        install(release)
    assert (target / "SKILL.md").read_text() == "user edits after interruption\n"
    assert (release[1] / installer.TRANSACTION).exists()
    assert active_runtime(release[1]).is_dir()


def test_recovery_does_not_remove_runtime_without_ownership_token(release):
    install(release)
    next_release(release)
    crash_after_write(release, "SKILL.md")
    transaction = json.loads((release[1] / installer.TRANSACTION).read_text())
    destination = release[1] / installer.SKILL_NAME / "releases" / transaction["release"]
    (destination / installer.OWNER).write_text("not this transaction")
    with pytest.raises(installer.InstallError, match="不属于本次安装"):
        install(release)
    assert destination.is_dir()
    assert active_runtime(release[1]).is_dir()


@pytest.mark.parametrize("changed", ["scripts/video_kb.py", installer.MANIFEST])
def test_source_change_after_inspection_never_runs_or_replaces_old_runtime(release, changed):
    install(release)
    target = release[1] / installer.SKILL_NAME
    before_marker = (target / installer.MARKER).read_bytes()
    before_runtime = active_runtime(release[1])
    before_skill = (target / "SKILL.md").read_bytes()
    next_release(release)
    calls = []
    def change_after_inspection(args, **kwargs):
        calls.append(args)
        if args[1] == "-c":
            path = release[0] / changed
            path.write_bytes(path.read_bytes() + b"\n# changed after inspection\n")
        return offline_runner(args, **kwargs)
    with pytest.raises(installer.InstallError, match="安装期间发生变化"):
        install(release, runner=change_after_inspection)
    assert len(calls) == 1 and calls[0][1] == "-c"
    assert (target / installer.MARKER).read_bytes() == before_marker
    assert (target / "SKILL.md").read_bytes() == before_skill
    assert active_runtime(release[1]) == before_runtime and before_runtime.is_dir()
    assert not (release[1] / installer.TRANSACTION).exists()
    assert list((target / "releases").iterdir()) == [before_runtime]


def test_bootstrap_changed_after_inspection_is_rejected_before_snapshot(release, monkeypatch):
    install(release)
    target = release[1] / installer.SKILL_NAME
    before_marker = (target / installer.MARKER).read_bytes()
    before_skill = (target / "SKILL.md").read_bytes()
    before_runtime = active_runtime(release[1])
    next_release(release)
    original = installer.inspect_release
    def inspect_then_change(source):
        result = original(source)
        (source / "skills/feishu-video-to-notion/SKILL.md").write_text("unverified instructions")
        return result
    monkeypatch.setattr(installer, "inspect_release", inspect_then_change)
    calls = []
    with pytest.raises(installer.InstallError, match="安装期间发生变化"):
        install(release, runner=lambda args, **kwargs: calls.append(args))
    assert calls == []
    assert (target / installer.MARKER).read_bytes() == before_marker
    assert (target / "SKILL.md").read_bytes() == before_skill
    assert before_runtime.is_dir()
    assert not (release[1] / installer.TRANSACTION).exists()
