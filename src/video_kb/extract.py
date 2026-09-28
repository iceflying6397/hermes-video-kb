"""Evidence-conscious platform adapters; never infer a video transcript from metadata."""
from __future__ import annotations

import html
import json
import re
import tempfile
from html.parser import HTMLParser
from pathlib import Path

from .network import SourceError, fetch_source, normalize_source_url, validate_source_url, source_identity
from .transcripts import embedded_transcript
from .media import MediaRef, download_media, prepare_audio, validate_media_url, prepare_cache, mark_cache_owner
from .asr import ASRUnavailable, check_asr, transcribe_audio


class _Page(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.meta = {}
        self.title = []
        self.body = []
        self.scripts = []
        self.stack = []
        self.article_depth = None
        self.script = None
        self.ignored = 0

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "meta":
            key = values.get("property") or values.get("name")
            if key and values.get("content"):
                self.meta.setdefault(key.lower(), values["content"])
        # HTMLParser does not balance void tags.
        if tag in {"meta", "link", "img", "br", "hr", "input", "source", "wbr", "area", "base", "embed", "param", "track", "col"}:
            if tag == "br" and self.article_depth is not None:
                self.body.append("\n")
            return
        self.stack.append(tag)
        if values.get("id") == "js_content" and self.article_depth is None:
            self.article_depth = len(self.stack)
        if tag in {"script", "style", "noscript"}:
            self.ignored += 1
        if tag == "script":
            self.script = []

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if self.stack and self.stack[-1] == tag:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if tag == "script" and self.script is not None:
            self.scripts.append("".join(self.script))
            self.script = None
        if tag in {"script", "style", "noscript"}:
            self.ignored = max(0, self.ignored - 1)
        if tag in self.stack:
            index = len(self.stack) - 1 - self.stack[::-1].index(tag)
            if self.article_depth is not None and index + 1 <= self.article_depth:
                self.article_depth = None
            self.stack = self.stack[:index]
        if self.article_depth is not None and tag in {"p", "div", "section", "h1", "h2", "h3", "li"}:
            self.body.append("\n")

    def handle_data(self, data):
        if self.script is not None:
            self.script.append(data)
        if not self.ignored and self.stack and self.stack[-1] == "title":
            self.title.append(data)
        if self.article_depth is not None and not self.ignored:
            self.body.append(data)


def _clean(value, limit):
    if not isinstance(value, str):
        return ""
    value = html.unescape(value)
    value = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", value)
    value = re.sub(r"[ \t]+", " ", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()[:limit]


def _json_undefined_to_null(text):
    # XHS embeds JSON with JavaScript `undefined` values. Convert only that one
    # literal outside quoted strings; every other JS construct stays invalid.
    output, quoted, escaped, i = [], False, False, 0
    while i < len(text):
        char = text[i]
        if quoted:
            output.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
            i += 1
        elif char == '"':
            quoted = True
            output.append(char)
            i += 1
        elif text.startswith("undefined", i) and (i == 0 or not (text[i - 1].isalnum() or text[i - 1] in "_$")) and (i + 9 == len(text) or not (text[i + 9].isalnum() or text[i + 9] in "_$")):
            output.append("null")
            i += 9
        else:
            output.append(char)
            i += 1
    return "".join(output)


def _xhs_state(scripts):
    decoder = json.JSONDecoder()
    for script in scripts:
        # Only JSON is parsed. No JS evaluation, no regex-based arbitrary-object expansion.
        marker = re.search(r"(?:window\.)?__INITIAL_STATE__\s*=\s*", script)
        if marker:
            candidate = _json_undefined_to_null(script[marker.end():].lstrip())
            try:
                value, _ = decoder.raw_decode(candidate)
            except (ValueError, RecursionError):
                continue
            if isinstance(value, dict):
                return value
    return {}


def _xhs_note(state, url):
    note_root = state.get("note")
    details = note_root.get("noteDetailMap") if isinstance(note_root, dict) else None
    if not isinstance(details, dict):
        return {}
    # Only accept the note matching the requested ID, never another recommended post.
    identifier = url.split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1]
    selected = details.get(identifier)
    if not isinstance(selected, dict):
        return {}
    note = selected.get("note")
    if not isinstance(note, dict) or (note.get("noteId") and note.get("noteId") != identifier):
        return {}
    return note


def _douyin_item(scripts, url):
    """Read only the requested aweme_id, never a recommended item's video."""
    identifier = normalize_source_url(url).rsplit("/", 1)[-1]
    for script in scripts:
        marker = re.search(r"(?:window\.)?_ROUTER_DATA\s*=\s*", script)
        if not marker:
            continue
        try:
            state, _ = json.JSONDecoder().raw_decode(script[marker.end():].lstrip())
        except (ValueError, RecursionError):
            continue
        stack, visited = [(state, 0)], 0
        while stack and visited < 5000:
            item, depth = stack.pop()
            visited += 1
            if depth > 16:
                continue
            if isinstance(item, dict):
                if str(item.get("aweme_id", "")) == identifier:
                    return item
                stack.extend((v, depth + 1) for v in item.values() if isinstance(v, (dict, list)))
            elif isinstance(item, list):
                stack.extend((v, depth + 1) for v in item[:500])
    return {}


def _media_ref(platform, source_url, note):
    """Only use media fields inside the already selected source object."""
    urls = []
    video = note.get("video") if isinstance(note, dict) else None
    if isinstance(video, dict) and platform == "xiaohongshu":
        media = video.get("media")
        streams = media.get("stream") if isinstance(media, dict) else None
        if isinstance(streams, dict):
            for codec in ("h264", "h265", "av1"):
                variants = streams.get(codec)
                if isinstance(variants, list):
                    for variant in variants[:10]:
                        if isinstance(variant, dict):
                            urls.append(variant.get("masterUrl"))
        for name in ("url", "masterUrl"):
            urls.append(video.get(name))
    elif isinstance(video, dict) and platform == "douyin":
        address = video.get("play_addr")
        if isinstance(address, dict) and isinstance(address.get("url_list"), list):
            urls.extend(address["url_list"][:10])
    for url in urls:
        if not isinstance(url, str):
            continue
        # Upgrade public platform media to TLS; never fetch HTTP first.
        if url.startswith("http://"):
            url = "https://" + url[7:]
        ref = MediaRef(source_url, url)
        try:
            validate_media_url(ref)
            return ref
        except SourceError:
            continue
    return None


def _transcribe_media(ref, cache_dir, config, resolver):
    capability = check_asr(config=config)
    if not capability["available"]:
        # Keep the queued job retryable; do not silently save a bookmark as completion.
        raise SourceError("已找到本条视频的媒体。" + capability["message"])
    prepare_cache(cache_dir)
    try:
        with tempfile.TemporaryDirectory(prefix="media-", dir=cache_dir) as folder:
            root = Path(folder)
            mark_cache_owner(root)
            download_media(ref, root / "source.mp4", resolver=resolver)
            duration = prepare_audio(root / "source.mp4", root / "audio.wav")
            data = transcribe_audio(root / "audio.wav", duration=duration, config=config)
    except ASRUnavailable as exc:
        raise SourceError(str(exc)) from None
    return data


def extract_url(url: str, *, cache_dir: Path, config: dict | None = None) -> dict:
    """Read captions first, otherwise source-bound media and existing offline ASR."""
    config = config or {}
    resolver = None
    if config.get("public_dns_fallback") is True:
        from .public_dns import resolve_public as resolver
    platform = validate_source_url(url)
    # Validate canonicalization before network traffic, including duplicate identity fields.
    normalize_source_url(url)
    fetched = fetch_source(url, **({"resolver": resolver} if resolver else {}))
    canonical = normalize_source_url(fetched.url)
    page = _Page()
    try:
        page.feed(fetched.body.decode("utf-8", errors="replace"))
        page.close()
    except (ValueError, RecursionError):
        raise SourceError("页面格式无法安全读取。") from None
    title = _clean(page.meta.get("og:title") or "".join(page.title), 300) or "待补充标题"
    description = _clean(page.meta.get("og:description") or page.meta.get("description"), 50000)
    card = dict(source_url=url, canonical_url=canonical, title=title, platform=platform,
                content_type="video" if platform == "douyin" else "article" if platform == "wechat" else "note",
                author=_clean(page.meta.get("author"), 200), summary="", points=[], actions=[], tags=[],
                source_text="", evidence="metadata" if description or title != "待补充标题" else "none",
                evidence_note="只取得标题或页面介绍，没有取得视频逐字稿；需要人工核对。")
    if description:
        card["evidence_note"] += " 页面介绍（不是正文）：" + description[:600]
    note = {}
    if platform == "wechat":
        text = _clean("".join(page.body), 50001)
        if text:
            card.update(source_text=text[:50000], evidence="partial" if len(text) > 50000 else "full_text",
                        evidence_note="已读取公众号公开文章正文。" if len(text) <= 50000 else "文章较长，只保留前 50000 字；摘要不代表全文。")
        else:
            card["evidence_note"] = "未取得文章正文，可能需要登录或通过平台验证；只保存链接和页面信息。"
    elif platform == "xiaohongshu":
        note = _xhs_note(_xhs_state(page.scripts), fetched.url)
        if note:
            card["title"] = _clean(note.get("title"), 300) or title
            user = note.get("user")
            card["author"] = _clean(user.get("nickname"), 200) if isinstance(user, dict) else ""
            card["content_type"] = "video" if note.get("type") == "video" else "note"
            text = _clean(note.get("desc"), 50001)
            if text:
                video = card["content_type"] == "video"
                card.update(source_text=text[:50000], evidence="partial" if video or len(text) > 50000 else "full_text",
                            evidence_note="仅取得视频配文，没有取得视频逐字稿；摘要只针对配文。" if video else "已取得笔记文字；没有识别图片内文字。")
                if len(text) > 50000:
                    card["evidence_note"] += " 文字超过 50000 字，已截断。"
        else:
            card["evidence_note"] = "未取得笔记正文，可能需要登录或通过平台验证；只保存链接和页面信息。"
    elif platform == "douyin":
        note = _douyin_item(page.scripts, fetched.url)
        if note:
            card["content_type"] = "video" if isinstance(note.get("video"), dict) else "note"
            card["title"] = _clean(note.get("desc"), 300) or title
            user = note.get("author")
            card["author"] = _clean(user.get("nickname"), 200) if isinstance(user, dict) else ""
            text = _clean(note.get("desc"), 50000)
            if text:
                card.update(source_text=text, evidence="partial", evidence_note="仅取得视频配文，没有取得视频逐字稿；摘要只针对配文。")
    if card["content_type"] == "video":
        transcript = _clean(embedded_transcript(page.scripts, fetched.url), 50001) if source_identity(fetched.url) else ""
        if transcript:
            card.update(source_text=transcript[:50000], evidence="partial",
                        evidence_note="取得页面公开标注的视频文字稿；未验证是否覆盖所有声音，摘要仅基于这份文字稿。" + (" 文字过长已截断。" if len(transcript) > 50000 else ""))
        elif ref := _media_ref(platform, fetched.url, note):
            data = _transcribe_media(ref, cache_dir, config, resolver)
            text = _clean(data["text"], 50001)
            limited = bool(data.get("truncated")) or bool(data.get("duration_mismatch")) or len(text) > 50000
            card.update(source_text=text[:50000], evidence="partial" if limited else "asr",
                        evidence_note=("ASR转写，非官方字幕。来自本条笔记的公开媒体音轨；本机离线识别，未上传音频。"
                                       f"音轨约 {data['audio_seconds']:.1f} 秒；自动识别可能漏字、误认同音词和专名，未逐字人工校验。"
                                       + ("音轨与视频时长存在明显差异，可能缺少部分声音，不能称为完整视频原文。" if data.get("duration_mismatch") else "")
                                       + ("文字超过限制，只保留前 50000 字。" if data.get("truncated") or len(text) > 50000 else "已处理整段可取得音轨；识别结束不代表讲话逐字无误。")))
    return card
