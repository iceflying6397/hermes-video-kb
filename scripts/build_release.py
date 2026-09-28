#!/usr/bin/env python3
"""Build a deterministic distribution archive from an explicit source allowlist."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import zipfile

TOP_LEVEL = {"README.md", "QUICKSTART.md", "LICENSE", "VERSION", "requirements.lock", "requirements-asr.lock", "pyproject.toml"}
ALLOWED_SUFFIXES = {"scripts": {".py"}, "src": {".py"}, "skills": {".md", ".yaml"}, "docs": {".md"}, "prompts": {".md"}}


def release_files(source: Path) -> list[Path]:
    result = []
    for path in source.iterdir():
        if path.name in TOP_LEVEL:
            if path.is_symlink() or not path.is_file():
                raise ValueError("发布文件必须是普通文件。")
            result.append(path)
        elif path.name in ALLOWED_SUFFIXES:
            if path.is_symlink() or not path.is_dir():
                raise ValueError("发布文件夹必须是普通文件夹。")
            for child in path.rglob("*"):
                if child.is_symlink():
                    raise ValueError("发布目录不允许符号链接。")
                relative = child.relative_to(path)
                if any(part.startswith(".") or part == "__pycache__" for part in relative.parts):
                    continue
                if child.is_file() and child.suffix in ALLOWED_SUFFIXES[path.name]:
                    result.append(child)
    return sorted(result, key=lambda path: path.relative_to(source).as_posix())


def build_manifest(source: Path) -> dict:
    version = (source / "VERSION").read_text("utf-8").strip()
    if not re.fullmatch(r"\d+\.\d+\.\d+(?:-[a-z0-9.]+)?", version):
        raise ValueError("版本号无效。")
    files = release_files(source)
    if sum(path.stat().st_size for path in files) > 20 * 1024 * 1024:
        raise ValueError("发布内容异常过大。")
    return {"format": 1, "product": "hermes-video-kb", "version": version,
            "files": {path.relative_to(source).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest() for path in files}}


def build(source: Path, output: Path | None = None) -> dict:
    source = source.resolve()
    manifest = build_manifest(source)
    encoded = (json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    (source / "RELEASE_MANIFEST.json").write_bytes(encoded)
    result = {"version": manifest["version"], "files": len(manifest["files"]), "manifest": str(source / "RELEASE_MANIFEST.json")}
    if output is not None:
        output = output.absolute()
        if output.exists():
            raise ValueError("输出文件已存在，请换一个文件名以保留旧发布包。")
        output.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(output, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for name in [*manifest["files"], "RELEASE_MANIFEST.json"]:
                data = encoded if name == "RELEASE_MANIFEST.json" else (source / name).read_bytes()
                entry = zipfile.ZipInfo(f"hermes-video-kb-{manifest['version']}/{name}", date_time=(2026, 1, 1, 0, 0, 0))
                entry.compress_type = zipfile.ZIP_DEFLATED
                entry.create_system = 3
                entry.external_attr = 0o100644 << 16
                archive.writestr(entry, data)
        result.update(archive=str(output), sha256=hashlib.sha256(output.read_bytes()).hexdigest())
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="生成不含个人数据的发布包；省略 --output 时只更新清单。")
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        result = build(args.source, args.output)
    except (ValueError, OSError) as error:
        print(json.dumps({"ok": False, "message": str(error)}, ensure_ascii=False))
        return 1
    print(json.dumps({"ok": True, **result}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
