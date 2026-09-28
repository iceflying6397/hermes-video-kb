"""Private, crash-safe local queue. No credential values are returned to callers."""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import fcntl
import hashlib
import json
import os
import stat
import tempfile
import time


class StateError(RuntimeError):
    pass


MAX_STATE_BYTES = 32 * 1024 * 1024
MAX_JOBS = 20000
MAX_PENDING = 100


def default_state_dir() -> Path:
    base = Path.home() / ".local" / "share" / "hermes-video-kb"
    profile = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))).expanduser().absolute()
    return base / hashlib.sha256(str(profile).encode()).hexdigest()[:16]


def private_dir(path: Path) -> Path:
    path = path.expanduser().absolute()
    # Never follow a symlink into somebody else's data.
    for component in [*reversed(path.parents), path]:
        if component.is_symlink():
            raise StateError("本地保存位置包含快捷链接，请换一个真实文件夹。")
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise StateError("本地保存位置不属于当前用户。")
    path.chmod(0o700)
    return path


def safe_file(path: Path) -> None:
    if path.is_symlink():
        raise StateError("本地记录被替换为快捷链接，已停止操作。")
    try:
        info = path.stat()
    except FileNotFoundError:
        return
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
        raise StateError("本地记录权限异常，已停止操作。")


def atomic_json(path: Path, value: dict) -> None:
    safe_file(path)
    data = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
    if len(data) > MAX_STATE_BYTES:
        raise StateError("待处理记录已达到本地容量上限，请先处理已有任务。")
    fd, temp = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as out:
            os.fchmod(out.fileno(), 0o600)
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temp, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def job_key(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()


class Store:
    def __init__(self, root: Path):
        self.root = private_dir(root)
        self.path = self.root / "state.json"
        self.data = None

    @contextmanager
    def locked(self):
        lock_path = self.root / "queue.lock"
        safe_file(lock_path)
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise StateError("上一项任务仍在处理，请稍后再试。") from None
            self.load()
            yield self
        finally:
            os.close(fd)

    def load(self):
        safe_file(self.path)
        if not self.path.exists():
            self.data = {"version": 2, "paused": False, "connected": False,
                         "binding": None, "library_attempt": None, "jobs": {}}
            return
        try:
            if self.path.stat().st_size > MAX_STATE_BYTES:
                raise ValueError()
            self.path.chmod(0o600)
            self.data = json.loads(self.path.read_text())
            if self.data.get("version") != 2 or not isinstance(self.data.get("jobs"), dict):
                raise ValueError()
            changed = False
            for item in self.data["jobs"].values():
                if item["status"] == "writing":
                    item["status"] = "uncertain"
                    changed = True
                elif item["status"] == "processing":
                    item["status"] = "queued"
                    changed = True
            if (self.data.get("library_attempt") or {}).get("status") == "writing":
                self.data["library_attempt"]["status"] = "uncertain"
                changed = True
            if changed:
                self.save()
        except (ValueError, TypeError, KeyError, AttributeError):
            raise StateError("本地记录损坏，已保留原文件并停止操作，请勿重新初始化覆盖。") from None

    def save(self):
        atomic_json(self.path, self.data)

    def enqueue(self, url: str, *, variant: str = "") -> tuple[str, dict]:
        key = job_key(url + variant)
        jobs = self.data["jobs"]
        if key in jobs:
            return key, jobs[key]
        if len(jobs) >= MAX_JOBS:
            raise StateError("当前配置已保存 20000 个来源，达到此版本容量上限。请保留本地记录以防重复，并在独立 Hermes 配置建立另一个知识库。")
        if sum(j["status"] != "saved" for j in jobs.values()) >= MAX_PENDING:
            raise StateError("已有 100 条待处理任务，请先完成或核对这些任务。")
        item = {"url": url, "status": "queued", "created_at": time.time(), "tries": 0}
        jobs[key] = item
        self.save()
        return key, item
