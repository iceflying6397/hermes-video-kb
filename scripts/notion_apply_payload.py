#!/usr/bin/env python3
"""Retired: no environment, credential or network access."""
import json

if __name__ == "__main__":
    print(json.dumps({"ok": False, "message": '旧版通用 Notion 写入工具已停用。请使用视频知识库助手的 collect 命令。无需提供 Notion 密钥。'}, ensure_ascii=False))
    raise SystemExit(2)
