#!/usr/bin/env python3
"""Install this release into one owned Hermes skill directory. Standard library only."""
from __future__ import annotations

import argparse
import base64
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import uuid
from typing import Callable

SKILL_NAME = "feishu-video-to-notion"
MARKER = ".video-kb-install.json"
MANIFEST = "RELEASE_MANIFEST.json"
PRODUCT = "hermes-video-kb"
MAX_RELEASE_BYTES = 20 * 1024 * 1024
TRANSACTION = f".{SKILL_NAME}.install-transaction.json"
OWNER = ".video-kb-release-owner"
LAUNCHER = '''#!/usr/bin/env python3
"""Launch only the locally installed, pinned video knowledge-base runtime."""
import json
import os
from pathlib import Path
import re
import sys

root = Path(__file__).resolve().parent.parent
try:
    marker = json.loads((root / ".video-kb-install.json").read_text("utf-8"))
    release = marker["release"]
    if marker.get("product") != "hermes-video-kb" or not re.fullmatch(r"[a-f0-9]{64}", release):
        raise ValueError()
    runtime = root / "releases" / release
    if runtime.is_symlink() or not runtime.is_dir():
        raise ValueError()
    python = runtime / ".venv" / "bin" / "python"
    entry = runtime / "scripts" / "video_kb.py"
    os.execv(str(python), [str(python), str(entry), *sys.argv[1:]])
except (OSError, ValueError, KeyError, TypeError):
    print('{"ok":false,"message":"安装不完整，请重新安装此 Skill；知识库和本地记录不会被删除。"}')
    sys.exit(1)
'''


class InstallError(Exception):
    pass


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _no_symlink_parents(path: Path) -> None:
    path = path.absolute()
    for item in [path, *path.parents]:
        if item.is_symlink():
            raise InstallError("安装路径包含符号链接，请选择普通文件夹。")


def _relative(value: str) -> Path:
    if not isinstance(value, str) or "\\" in value or "\x00" in value:
        raise InstallError("发布清单含有不安全路径。")
    parsed = PurePosixPath(value)
    if parsed.is_absolute() or not parsed.parts or any(p in ("..", ".") for p in parsed.parts):
        raise InstallError("发布清单含有不安全路径。")
    if str(parsed) != value or value.startswith("."):
        raise InstallError("发布清单含有不安全路径。")
    return Path(*parsed.parts)


def inspect_release(source: Path) -> tuple[dict, str]:
    _no_symlink_parents(source)
    manifest_path = source / MANIFEST
    if not manifest_path.is_file() or manifest_path.is_symlink():
        raise InstallError("缺少发布校验清单。请使用正式发布包，或先生成发布清单。")
    if manifest_path.stat().st_size > 1024 * 1024:
        raise InstallError("发布校验清单过大。")
    raw = manifest_path.read_bytes()
    try:
        manifest = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as exc:
        raise InstallError("发布校验清单损坏。") from exc
    if not isinstance(manifest, dict) or manifest.get("product") != PRODUCT or manifest.get("format") != 1:
        raise InstallError("这不是受支持的发布包。")
    files = manifest.get("files")
    if not isinstance(files, dict) or not files or len(files) > 500:
        raise InstallError("发布校验清单无效。")
    required = {"VERSION", "requirements.lock", "scripts/video_kb.py", "src/video_kb/__init__.py", f"skills/{SKILL_NAME}/SKILL.md"}
    if not required.issubset(files):
        raise InstallError("发布包缺少必要文件。")
    total = 0
    for name, expected in files.items():
        relative = _relative(name)
        if not isinstance(expected, str) or not re.fullmatch(r"[a-f0-9]{64}", expected):
            raise InstallError("发布校验值无效。")
        path = source / relative
        _no_symlink_parents(path)
        if not path.is_file():
            raise InstallError("发布包缺少文件。")
        total += path.stat().st_size
        if total > MAX_RELEASE_BYTES or _digest(path) != expected:
            raise InstallError("发布包校验失败，请重新下载。")
    return manifest, hashlib.sha256(raw).hexdigest()


def _atomic_write(path: Path, content: bytes, *, token: str | None = None) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".pending-{token}-" if token else ".pending-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        _sync_dir(path.parent)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _sync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


@contextmanager
def _installation_lock(skills_dir: Path):
    """The lock inode stays outside the directory being installed or removed."""
    import fcntl
    skills_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = skills_dir / f".{SKILL_NAME}.install.lock"
    _no_symlink_parents(path)
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
            raise InstallError("安装锁文件异常，已停止以保护原文件。")
        os.fchmod(fd, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise InstallError("另一个安装仍在进行，请完成后重试。") from None
        yield
    finally:
        os.close(fd)


def _recover_transaction(skills_dir: Path) -> None:
    """Finish or undo only this installer's interrupted, journalled changes."""
    journal = skills_dir / TRANSACTION
    if not journal.exists() and not journal.is_symlink():
        return
    _no_symlink_parents(journal)
    try:
        info = journal.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1 or info.st_size > 30 * 1024 * 1024:
            raise ValueError()
        tx = json.loads(journal.read_text("utf-8"))
        if tx["product"] != PRODUCT or tx["format"] != 1 or not re.fullmatch(r"[a-f0-9]{32}", tx["token"]):
            raise ValueError()
        if not re.fullmatch(r"[a-f0-9]{64}", tx["release"]):
            raise ValueError()
        old_files = {name: base64.b64decode(value, validate=True) for name, value in tx["old_files"].items()}
        new_hashes = tx["new_hashes"]
        old_marker = base64.b64decode(tx["old_marker"], validate=True) if tx["old_marker"] is not None else None
        new_marker = base64.b64decode(tx["new_marker"], validate=True)
        if not isinstance(new_hashes, dict) or not new_hashes:
            raise ValueError()
        for name in old_files.keys() | new_hashes.keys():
            _relative(name)
            if name not in {"SKILL.md", "scripts/video_kb.py"} and not name.startswith("references/"):
                raise ValueError()
        if any(not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value) for value in new_hashes.values()):
            raise ValueError()
        new_state = json.loads(new_marker)
        if (new_state.get("product") != PRODUCT or new_state.get("release") != tx["release"]
                or new_state.get("bootstrap") != new_hashes):
            raise ValueError()
        if old_marker is None:
            if old_files:
                raise ValueError()
        else:
            old_state = json.loads(old_marker)
            if (old_state.get("product") != PRODUCT
                    or old_state.get("bootstrap") != {name: hashlib.sha256(content).hexdigest() for name, content in old_files.items()}):
                raise ValueError()
    except (ValueError, TypeError, KeyError, AttributeError):
        raise InstallError("上次安装的恢复记录异常，原文件已保留，请勿删除恢复记录。") from None
    target = skills_dir / SKILL_NAME
    _no_symlink_parents(target)
    marker = target / MARKER
    _no_symlink_parents(marker)
    current_marker = marker.read_bytes() if marker.is_file() else None
    if current_marker not in (old_marker, new_marker):
        raise InstallError("安装中断后安装记录被修改，已保留所有文件，请先核对修改。")
    activated = current_marker == new_marker
    # A user edit never gets mistaken for a partially committed installer write.
    for name in old_files.keys() | new_hashes.keys():
        path = target / name
        _no_symlink_parents(path)
        allowed = {new_hashes[name]} if activated and name in new_hashes else set()
        if not activated:
            if name in old_files:
                allowed.add(hashlib.sha256(old_files[name]).hexdigest())
            if name in new_hashes:
                allowed.add(new_hashes[name])
        if path.exists():
            if not path.is_file() or _digest(path) not in allowed:
                raise InstallError("安装中断后 Skill 文件被修改，已保留修改和恢复记录，请先核对。")
        elif (activated and name in new_hashes) or (not activated and name in old_files and name in new_hashes):
            raise InstallError("安装中断后原文件缺失，已保留恢复记录，请先核对。")
    destination = target / "releases" / tx["release"]
    stage = target / "releases" / f".install-{tx['token']}"
    for runtime in (stage, destination):
        _no_symlink_parents(runtime)
        if runtime.exists():
            owner = runtime / OWNER
            _no_symlink_parents(owner)
            # The token moves with the directory, covering a kill just after rename.
            if not runtime.is_dir() or not owner.is_file() or owner.read_text("ascii") != tx["token"]:
                if (runtime == stage and runtime.is_dir() and not owner.exists()
                        and all(p.name.startswith(f".pending-{tx['token']}-") and p.is_file()
                                for p in runtime.iterdir())):
                    continue
                raise InstallError("未完成的版本目录不属于本次安装，已保留现场。")
    if activated and not destination.is_dir():
        raise InstallError("新安装的运行目录缺失，已保留恢复记录，请先核对。")
    if not activated:
        for name, content in old_files.items():
            _atomic_write(target / name, content, token=tx["token"])
        for name in new_hashes.keys() - old_files.keys():
            path = target / name
            if path.exists():
                path.unlink()
        if destination.exists():
            # Ownership was checked above; never remove an activated runtime.
            shutil.rmtree(destination)
    if stage.exists():
        shutil.rmtree(stage)
    if target.exists():
        for pending in target.rglob(f".pending-{tx['token']}-*"):
            _no_symlink_parents(pending)
            if pending.is_file():
                pending.unlink()
        if old_marker is None and not activated:
            for directory in sorted((p for p in target.rglob("*") if p.is_dir() and not p.is_symlink()), key=lambda p: len(p.parts), reverse=True):
                try:
                    directory.rmdir()
                except OSError:
                    pass
            try:
                target.rmdir()
            except OSError:
                pass  # Preserve any files added by the user during installation.
    journal.unlink()
    _sync_dir(skills_dir)


def _bootstrap(source: Path, manifest: dict) -> dict[str, bytes]:
    prefix = f"skills/{SKILL_NAME}/"
    result = {}
    for name in manifest["files"]:
        if name.startswith(prefix):
            relative = name[len(prefix):]
            if relative == "SKILL.md" or relative.startswith("references/"):
                content = (source / name).read_bytes()
                if hashlib.sha256(content).hexdigest() != manifest["files"][name]:
                    raise InstallError("发布包在安装期间发生变化，请重新核对发布包后重试。")
                result[relative] = content
    result["scripts/video_kb.py"] = LAUNCHER.encode("utf-8")
    return result


def _read_install(target: Path) -> dict | None:
    if not target.exists():
        return None
    _no_symlink_parents(target)
    marker = target / MARKER
    if not marker.is_file() or marker.is_symlink() or marker.stat().st_size > 100_000:
        raise InstallError("同名 Skill 已存在且不是本安装器管理的内容；为保护原文件，安装已停止。")
    try:
        state = json.loads(marker.read_text("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise InstallError("原安装记录损坏，安装已停止；原文件保留。") from exc
    if state.get("product") != PRODUCT or not re.fullmatch(r"[a-f0-9]{64}", state.get("release", "")):
        raise InstallError("原安装记录不匹配，安装已停止。")
    if "local_asr" in state and not isinstance(state["local_asr"], bool):
        raise InstallError("原安装的转写组件记录异常，安装已停止；原文件保留。")
    hashes = state.get("bootstrap")
    if not isinstance(hashes, dict) or not hashes:
        raise InstallError("原安装记录不完整，安装已停止。")
    for name, digest in hashes.items():
        path = target / _relative(name)
        _no_symlink_parents(path)
        if not path.is_file() or _digest(path) != digest:
            raise InstallError("这个 Skill 的说明或启动文件被修改过；为保护修改，安装已停止。")
    allowed = set(hashes) | {MARKER}
    for path in target.rglob("*"):
        relative = path.relative_to(target).as_posix()
        if path.is_symlink():
            # Python venv itself legitimately contains interpreter symlinks.
            if not relative.startswith("releases/"):
                raise InstallError("Skill 安装目录包含未知符号链接，安装已停止。")
        if path.is_file() and not relative.startswith("releases/") and relative not in allowed:
            raise InstallError("Skill 中存在额外文件；为保护用户内容，安装已停止。")
    releases = target / "releases"
    _no_symlink_parents(releases)
    if not (releases / state["release"]).is_dir() or (releases / state["release"]).is_symlink():
        raise InstallError("原安装版本目录异常，安装已停止。")
    return state


def install(source: Path, skills_dir: Path, python: Path, *, dry_run: bool = False,
            with_local_asr: bool | None = None, runner: Callable = subprocess.run) -> dict:
    if os.name != "posix":
        raise InstallError("这个发布包支持 macOS 和 Linux；当前系统暂不支持自动安装。")
    if with_local_asr is not None and not isinstance(with_local_asr, bool):
        raise InstallError("本地转写安装选项无效。")
    source, skills_dir, python = source.absolute(), skills_dir.expanduser().absolute(), python.expanduser().absolute()
    manifest, release = inspect_release(source)
    _no_symlink_parents(skills_dir)
    if not python.is_file() or not os.access(python, os.X_OK):
        raise InstallError("所选 Python 无法运行，请让 Hermes 选择已安装的 Python 3.11 或更新版本。")
    if dry_run:
        if (skills_dir / TRANSACTION).exists():
            raise InstallError("上次安装中断；正式运行安装会先恢复原版本。预览没有改动任何文件。")
        return _install_locked(source, skills_dir, python, manifest, release, dry_run=True,
                               with_local_asr=with_local_asr, runner=runner)
    with _installation_lock(skills_dir):
        _recover_transaction(skills_dir)
        return _install_locked(source, skills_dir, python, manifest, release, dry_run=False,
                               with_local_asr=with_local_asr, runner=runner)


def _install_locked(source: Path, skills_dir: Path, python: Path, manifest: dict, release: str, *,
                    dry_run: bool, with_local_asr: bool | None, runner: Callable) -> dict:
    target = skills_dir / SKILL_NAME
    previous = _read_install(target)
    local_asr = with_local_asr if with_local_asr is not None else bool(previous and previous.get("local_asr", False))
    manifest_sha256 = release
    if local_asr:
        if "requirements-asr.lock" not in manifest["files"]:
            raise InstallError("发布包没有经校验的本地转写组件清单，尚未安装或修改原版本。")
        # Same release can be installed with or without the optional wheels.
        # Preserve legacy core runtime IDs and never confuse the two profiles.
        release = hashlib.sha256((manifest_sha256 + "\0local-asr-v1").encode("ascii")).hexdigest()
    files = _bootstrap(source, manifest)
    disclosure = {
        "ok": True, "action": "install_preview" if dry_run else "installed",
        "message": "将安装视频知识库助手及同目录的私有安装锁和恢复记录；首次安装会从官方 PyPI 下载固定版本的运行组件。不会连接 Notion、读取密钥或改动其他 Skill。",
        "install_path": str(target), "version": manifest.get("version", ""),
        "local_asr": local_asr, "manifest_sha256": manifest_sha256,
        "required_python": ">=3.12" if local_asr else ">=3.11",
        "release": release, "already_installed": bool(previous and previous["release"] == release),
        "next": "安装后先运行 setup 检查模型并说明能力；仅 ready_to_connect 时按用户连接意图运行 setup --connect --open-browser。",
    }
    if local_asr:
        disclosure["message"] += " 本地转写组件需要 Python 3.12 或更新版本，同时从官方 PyPI 下载约 60–85 MB 组件；本次不下载识别模型、不发送音视频。以后本地识别会占用 CPU；可取消安装，失败时保留原运行版本。"
    if dry_run:
        return disclosure
    if previous and previous["release"] == release:
        disclosure["message"] = "此版本已安装；现有设置和记录保留。"
        return disclosure
    minimum = (3, 12) if local_asr else (3, 11)
    try:
        runner([str(python), "-c", f"import sys; raise SystemExit(0 if sys.version_info >= {minimum!r} else 1)"],
               check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except subprocess.CalledProcessError:
        message = ("本地转写组件需要 Python 3.12 或更新版本；请让助手选择本机已有的合适版本。"
                   if local_asr else "此助手需要 Python 3.11 或更新版本；请让助手选择本机已有的合适版本。")
        raise InstallError(message + "原安装保留，不会自动升级系统 Python。") from None
    releases = target / "releases"
    destination = releases / release
    if destination.exists():
        raise InstallError("发现未完成或重复的版本目录，已停止以保留现场。")
    old_files = {name: (target / name).read_bytes() for name in previous.get("bootstrap", {})} if previous else {}
    state = {"product": PRODUCT, "format": 1, "release": release,
             "local_asr": local_asr, "manifest_sha256": manifest_sha256,
             "previous_release": previous["release"] if previous else None,
             "version": manifest.get("version", ""),
             "bootstrap": {name: hashlib.sha256(content).hexdigest() for name, content in files.items()}}
    new_marker = (json.dumps(state, ensure_ascii=False, indent=2) + "\n").encode()
    token = uuid.uuid4().hex
    stage = releases / f".install-{token}"
    encode = lambda content: base64.b64encode(content).decode("ascii")
    transaction = {"product": PRODUCT, "format": 1, "token": token, "release": release,
                   "old_files": {name: encode(content) for name, content in old_files.items()},
                   "new_hashes": state["bootstrap"], "new_marker": encode(new_marker),
                   "old_marker": encode((target / MARKER).read_bytes()) if previous else None}
    # Persist recovery information before creating or changing any target files.
    _atomic_write(skills_dir / TRANSACTION, (json.dumps(transaction) + "\n").encode())
    try:
        target.mkdir(mode=0o700, exist_ok=True)
        releases.mkdir(mode=0o700, exist_ok=True)
        stage.mkdir(mode=0o700)
        _atomic_write(stage / OWNER, token.encode(), token=token)
        for name in manifest["files"]:
            path = stage / _relative(name)
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            shutil.copyfile(source / name, path)
            path.chmod(0o600)
            if _digest(path) != manifest["files"][name]:
                raise InstallError("发布包在安装期间发生变化，请重新核对发布包后重试。")
        shutil.copyfile(source / MANIFEST, stage / MANIFEST)
        if _digest(stage / MANIFEST) != manifest_sha256:
            raise InstallError("发布清单在安装期间发生变化，请重新核对发布包后重试。")
        runner([str(python), "-m", "venv", str(stage / ".venv")], check=True,
               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        runtime_python = stage / ".venv" / "bin" / "python"
        locks = ["requirements.lock", "requirements-asr.lock"] if local_asr else ["requirements.lock"]
        for lock in locks:
            runner([str(runtime_python), "-m", "pip", "install", "--isolated", "--disable-pip-version-check", "--no-input",
                    "--index-url", "https://pypi.org/simple", "--only-binary=:all:", "--require-hashes",
                    "-r", str(stage / lock)], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        runner([str(runtime_python), "-m", "pip", "check", "--isolated", "--disable-pip-version-check"],
               check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        # A failed environment never becomes the active runtime.
        runner([str(runtime_python), str(stage / "scripts" / "video_kb.py"), "--help"], check=True,
               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        os.replace(stage, destination)
        _sync_dir(releases)
        for name, content in files.items():
            _atomic_write(target / name, content, token=token)
        for name in set(old_files) - set(files):
            (target / name).unlink()
        _atomic_write(target / MARKER, new_marker, token=token)
        _recover_transaction(skills_dir)  # A committed marker means cleanup only.
        disclosure["message"] = "Skill 已安装。下一步先检查已有模型并说明当前可用范围，再连接 Notion；你的其他设置没有改变。"
        return disclosure
    except BaseException:
        _recover_transaction(skills_dir)
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="安装独立的视频知识库 Skill；--dry-run 只展示改动。")
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument("--skills-dir", type=Path, default=Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))) / "skills")
    parser.add_argument("--python", type=Path, required=True, help="已安装的 Python 3.11+ 的完整路径")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--with-local-asr", action="store_true", default=None,
                        help="经说明并同意后安装约 60–85 MB 本地转写组件（Python 3.12+）；不下载识别模型，升级默认保留原组件选择")
    args = parser.parse_args(argv)
    try:
        result = install(args.source, args.skills_dir, args.python, dry_run=args.dry_run,
                         with_local_asr=args.with_local_asr)
    except InstallError as error:
        print(json.dumps({"ok": False, "message": str(error)}, ensure_ascii=False))
        return 1
    except (OSError, subprocess.SubprocessError):
        print(json.dumps({"ok": False, "message": "安装未完成。原有 Skill、Notion 内容和本地记录均保留。请核实发布包完整、安装位置可写、Python 可运行且能访问官方 PyPI；不要为此关闭权限保护。"}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
