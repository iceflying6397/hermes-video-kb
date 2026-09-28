#!/usr/bin/env python3
"""Retired: no environment, credential or network access."""
import json

if __name__ == "__main__":
    print(json.dumps({"ok": False, "message": '旧版密钥验证工具已停用。请使用视频知识库助手的 status 命令；重新连接请先运行 setup 查看说明。'}, ensure_ascii=False))
    raise SystemExit(2)
