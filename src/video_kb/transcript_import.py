"""Import only a transcript file explicitly supplied by the user."""
import hashlib
import os
from pathlib import Path
import stat

from .cards import CardError, validate_card
from .network import validate_source_url, normalize_source_url


def import_transcript(url: str, path: Path) -> tuple[dict, str]:
    platform = validate_source_url(url)
    path = path.expanduser().absolute()
    if path.suffix.lower() not in {".txt", ".srt", ".vtt"} or any(p.is_symlink() for p in (path, *path.parents)):
        raise CardError("请提供普通的 TXT、SRT 或 VTT 字幕文件。")
    if any(part.startswith(".") for part in path.parts):
        raise CardError("不能把隐藏配置文件作为字幕导入。")
    try:
        # Validate the opened descriptor before reading. A FIFO/device supplied
        # as a .txt file must not block while holding the collection lock.
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as f:
            info = os.fstat(f.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > 200000:
                raise CardError("字幕文件过大或不是普通文件，请提供不超过 5 万字的字幕。")
            raw = f.read(200001)
        text = raw.decode("utf-8-sig").strip()
    except (OSError, UnicodeError):
        raise CardError("无法读取字幕，请提供 UTF-8 格式的文字文件。") from None
    if not text or len(text) > 50000 or "\x00" in text:
        raise CardError("字幕为空、过长或不是文字，请检查文件。")
    card = dict(source_url=url, canonical_url=normalize_source_url(url), title="用户提供的字幕笔记",
                platform=platform, content_type="video", author="", summary="", points=[], actions=[], tags=[],
                source_text=text, evidence="partial", evidence_note="用户提供的字幕或文字，尚未与原视频逐字核对；这是一条独立的补充笔记。")
    validate_card(card)
    return card, "\ntranscript:" + hashlib.sha256(text.encode()).hexdigest()
