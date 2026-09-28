import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from video_kb.cards import CardError, validate_card
from video_kb import summarize
from video_kb.network import normalize_source_url, validate_source_url


def test_wechat_reading_fragment_is_not_sent_or_used_as_identity():
    for url in ("https://mp.weixin.qq.com/s/abcdefghi", "https://mp.weixin.qq.com/s?__biz=abc&mid=1&idx=1&sn=def"):
        assert validate_source_url(url + "#rd") == "wechat"
        assert normalize_source_url(url + "#rd") == normalize_source_url(url)


def test_invalid_unicode_model_output_falls_back_without_poisoning_queue():
    card = dict(source_url="https://www.douyin.com/video/123456789", canonical_url="https://www.douyin.com/video/123456789", title="标题", platform="douyin", content_type="video", author="", summary="", points=[], actions=[], tags=[], source_text="依据", evidence="partial", evidence_note="依据未核验")
    payload = json.dumps(dict(summary="\ud800", points=[], actions=[], tags=[]))
    with patch.object(summarize, "complete_json", return_value=payload):
        out = summarize.summarize_card(card)
    validate_card(out)
    assert not out["summary"]
    json.dumps(out, ensure_ascii=False).encode("utf-8")
    for field in ("title", "source_text", "evidence_note"):
        with pytest.raises(CardError):
            validate_card({**card, field: "\ud800"})
    with pytest.raises(CardError):
        validate_card({**card, "points": ["\udfff"]})


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="POSIX FIFO")
def test_transcript_fifo_is_rejected_without_blocking(tmp_path):
    fifo = tmp_path / "input.txt"
    os.mkfifo(fifo)
    source = str(Path(__file__).resolve().parents[1] / "src")
    code = "import sys;sys.path.insert(0,sys.argv[1]);from video_kb.transcript_import import import_transcript;from video_kb.cards import CardError;from pathlib import Path\ntry: import_transcript('https://www.douyin.com/video/123456789',Path(sys.argv[2]))\nexcept CardError: sys.exit(0)\nelse: sys.exit(1)"
    out = subprocess.run([sys.executable, "-I", "-c", code, source, str(fifo)], capture_output=True, timeout=3)
    assert out.returncode == 0
