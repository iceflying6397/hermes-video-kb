"""Small profile-local operational settings, never derived from source material."""
from pathlib import Path
import json

from .state import StateError, safe_file, atomic_json, private_dir


def read_settings(root: Path) -> dict:
    path = root / "settings.json"
    safe_file(path)
    if not path.exists():
        return {}
    try:
        if path.stat().st_size > 1024:
            raise ValueError()
        data = json.loads(path.read_text("utf-8"))
        if set(data) != {"public_dns_fallback"} or type(data["public_dns_fallback"]) is not bool:
            raise ValueError()
        return data
    except (ValueError, TypeError, UnicodeError):
        raise StateError("本助手的网络设置格式异常，原文件已保留。") from None


def set_public_dns(root: Path, enabled: bool) -> dict:
    config = read_settings(root)
    config["public_dns_fallback"] = bool(enabled)
    atomic_json(private_dir(root) / "settings.json", config)
    return config
