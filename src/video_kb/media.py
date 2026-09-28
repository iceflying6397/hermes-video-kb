"""Source-bound MP4 download and offline decoding, never an arbitrary URL fetcher."""
from __future__ import annotations

from dataclasses import dataclass
import http.client
import json
import math
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import threading
import time
from urllib.parse import urljoin, urlsplit, urlunsplit

from .network import SourceError, _PinnedHTTPS, resolve_public, validate_source_url, source_identity

MAX_MEDIA_BYTES = 256 * 1024 * 1024
MAX_MEDIA_SECONDS = 1200
DOWNLOAD_SECONDS = 180
MAX_CACHE_BYTES = 640 * 1024 * 1024


def prepare_cache(cache_dir: Path) -> None:
    """Recover our dead-worker files; keep unknown content and bound total cache."""
    from .state import private_dir
    private_dir(cache_dir)
    used = 0
    entries = list(cache_dir.iterdir())
    if len(entries) > 128:
        raise SourceError("媒体缓存目录数量超限；已停止下载，需要先核对缓存。")
    for folder in entries:
        mode = folder.lstat().st_mode
        if not stat.S_ISDIR(mode):
            if stat.S_ISREG(mode):
                used += folder.stat().st_size
                continue
            raise SourceError("媒体缓存含符号链接或特殊文件，已停止下载。")
        files = list(folder.iterdir())
        if len(files) > 8 or any(not stat.S_ISREG(f.lstat().st_mode) for f in files):
            raise SourceError("媒体缓存含未知结构，已停止下载并保留原文件。")
        marker = folder / "owner.json"
        owned = False
        if folder.name.startswith("media-") and marker in files and marker.stat().st_size <= 300:
            try:
                value = json.loads(marker.read_text("utf-8"))
                pid = value.get("pid")
                owned = value.get("owner") == "hermes-video-kb-media-v1" and type(pid) is int and pid > 0 and {f.name for f in files} <= {"owner.json", "source.mp4", "audio.wav"}
                if owned:
                    try:
                        os.kill(pid, 0)
                    except ProcessLookupError:
                        # Only fixed files in a private, marked directory are ours to remove.
                        for f in files:
                            f.unlink()
                        folder.rmdir()
                        continue
                    except PermissionError:
                        pass
            except (OSError, ValueError, UnicodeError, AttributeError):
                owned = False
        used += sum(f.stat().st_size for f in files)
    if used + MAX_MEDIA_BYTES + 40 * 1024 * 1024 > MAX_CACHE_BYTES:
        raise SourceError("媒体缓存已接近 640 MB 上限，已停止新下载并保留任务；需要核对遗留文件后再试。")


def mark_cache_owner(folder: Path) -> None:
    marker = folder / "owner.json"
    with marker.open("x", encoding="utf-8") as out:
        os.chmod(marker, 0o600)
        json.dump({"owner": "hermes-video-kb-media-v1", "pid": os.getpid()}, out)


@dataclass(frozen=True)
class MediaRef:
    source_url: str
    url: str


def validate_media_url(ref: MediaRef) -> str:
    """The ref is produced only from the requested post's platform JSON object."""
    platform = validate_source_url(ref.source_url)
    if source_identity(ref.source_url) is None:
        raise SourceError("尚未确认视频来源，不能下载媒体。")
    value = ref.url
    if not isinstance(value, str) or len(value) > 8192 or re.search(r"[\x00-\x20\x7f\\]", value):
        raise SourceError("媒体地址格式异常。")
    try:
        p = urlsplit(value)
        host, port = p.hostname or "", p.port
    except ValueError:
        raise SourceError("媒体地址格式异常。") from None
    if p.scheme != "https" or p.username is not None or p.password is not None or p.fragment or port not in {None, 443} or p.netloc.lower() not in {host, host + ":443"}:
        raise SourceError("媒体必须使用公开 HTTPS 地址。")
    suffixes = {"xiaohongshu": ("xhscdn.com",), "douyin": ("douyinvod.com", "bytecdn.cn", "douyinpic.com")}.get(platform, ())
    allowed = any(host == s or host.endswith("." + s) for s in suffixes)
    # Official Douyin play endpoint redirects to the CDN; other site APIs are excluded.
    allowed = allowed or (platform == "douyin" and host in {"www.iesdouyin.com", "www.douyin.com"} and p.path.rstrip("/") == "/aweme/v1/play")
    if not allowed or re.search(r"\.(?:m3u8|mpd)(?:$|/)", p.path, re.I):
        raise SourceError("没有可核对归属的受支持直连媒体；不读取播放列表或任意网站。")
    return platform


def download_media(ref: MediaRef, destination: Path, *, resolver=None) -> None:
    """Stream one bounded MP4; each redirect gets fresh public-IP/TLS checks."""
    validate_media_url(ref)
    current = ref.url
    deadline = time.monotonic() + DOWNLOAD_SECONDS
    created_here = False
    try:
        # Caller creates a fresh private work directory. Never follow an existing file.
        with destination.open("xb") as out:
            created_here = True
            os.chmod(destination, 0o600)
            for hop in range(5):
                validate_media_url(MediaRef(ref.source_url, current))
                p = urlsplit(current)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise SourceError("媒体下载超过 3 分钟，已停止，可稍后重试。")
                address = (resolver or resolve_public)(p.hostname or "", timeout=remaining)[0]
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise SourceError("媒体下载超时。")
                connection = _PinnedHTTPS(p.hostname or "", address, min(remaining, 10.0))
                watchdog = threading.Timer(remaining, getattr(connection, "abort", connection.close))
                watchdog.daemon = True
                watchdog.start()
                try:
                    connection.request("GET", urlunsplit(("", "", p.path, p.query, "")), headers={
                        "User-Agent": "Mozilla/5.0 HermesVideoKB/2.1", "Accept": "video/mp4,audio/mp4,application/octet-stream",
                        "Accept-Encoding": "identity", "Connection": "close", "Referer": ref.source_url,
                    })
                    response = connection.getresponse()
                    if response.status in {301, 302, 303, 307, 308}:
                        location = response.getheader("Location")
                        if not location or hop == 4:
                            raise SourceError("媒体跳转过多或无效。")
                        current = urljoin(current, location)
                        continue
                    if response.status != 200:
                        raise SourceError("平台未提供可公开下载的媒体，可能已失效或需要登录。")
                    if (response.getheader("Content-Encoding") or "identity").lower().strip() != "identity":
                        raise SourceError("媒体压缩类型不受支持。")
                    kind = (response.getheader("Content-Type") or "").split(";", 1)[0].lower().strip()
                    if kind not in {"video/mp4", "audio/mp4", "application/octet-stream", "binary/octet-stream"}:
                        raise SourceError("媒体类型不受支持；目前只处理 MP4/M4A 直连文件。")
                    length = response.getheader("Content-Length")
                    if length and (not length.isdigit() or int(length) > MAX_MEDIA_BYTES):
                        raise SourceError("媒体超过 256 MB，已停止下载。")
                    count = 0
                    prefix = b""
                    while True:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise SourceError("媒体下载超时。")
                        if connection.sock is not None:
                            connection.sock.settimeout(min(remaining, 10.0))
                        chunk = response.read1(min(65536, MAX_MEDIA_BYTES - count + 1))
                        if not chunk:
                            break
                        count += len(chunk)
                        if count > MAX_MEDIA_BYTES:
                            raise SourceError("媒体超过 256 MB，已停止下载。")
                        prefix = (prefix + chunk)[:32]
                        out.write(chunk)
                    if len(prefix) < 12 or prefix[4:8] != b"ftyp":
                        raise SourceError("下载内容不是受支持的 MP4/M4A 文件。")
                    if length and count != int(length):
                        raise SourceError("媒体未下载完整，已保留任务供重试。")
                    return
                finally:
                    watchdog.cancel()
                    connection.close()
            raise SourceError("媒体跳转次数过多。")
    except (OSError, ValueError, http.client.HTTPException) as exc:
        if created_here:
            destination.unlink(missing_ok=True)
        if isinstance(exc, SourceError):
            raise
        raise SourceError("媒体暂时无法安全下载，请稍后重试。") from None


def local_environment() -> dict:
    # Decoders and ASR have no need for account keys, proxy credentials or hooks.
    env = {k: os.environ[k] for k in ("PATH", "HOME", "SYSTEMROOT", "TMPDIR", "LANG") if k in os.environ}
    env.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", OMP_NUM_THREADS="2", OPENBLAS_NUM_THREADS="2", VECLIB_MAXIMUM_THREADS="2")
    return env


def prepare_audio(media: Path, destination: Path) -> float:
    ffprobe, ffmpeg = shutil.which("ffprobe"), shutil.which("ffmpeg")
    if not ffprobe or not ffmpeg:
        raise SourceError("本机缺少音轨处理组件 ffmpeg/ffprobe；需说明下载范围后安装，任务已保留。")
    # Explicit MOV demuxer refuses playlists; external MP4 data references stay disabled.
    input_args = ["-protocol_whitelist", "file,pipe", "-f", "mov", "-enable_drefs", "0"]
    try:
        result = subprocess.run([ffprobe, "-v", "error", *input_args, "-show_entries", "format=duration:stream=codec_type,duration", "-of", "json", str(media)],
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, timeout=20, check=True, env=local_environment())
        if len(result.stdout) > 32000:
            raise ValueError()
        info = json.loads(result.stdout)
        duration = float(info["format"]["duration"])
        if not math.isfinite(duration) or duration <= 0 or duration > MAX_MEDIA_SECONDS:
            raise SourceError("视频时长异常或超过 20 分钟，已停止处理。")
        if not any(s.get("codec_type") == "audio" for s in info.get("streams", [])):
            raise SourceError("该视频没有可识别的音轨。")
        subprocess.run([ffmpeg, "-v", "error", "-nostdin", "-threads", "2", *input_args, "-i", str(media), "-map", "0:a:0", "-vn", "-ac", "1", "-ar", "16000", "-threads", "2", "-t", str(MAX_MEDIA_SECONDS), "-f", "wav", "-n", str(destination)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, timeout=90, check=True, env=local_environment())
        if destination.stat().st_size > 40 * 1024 * 1024:
            raise SourceError("音轨超出处理大小限制。")
        os.chmod(destination, 0o600)
        return duration
    except SourceError:
        raise
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        raise SourceError("媒体音轨格式异常或处理超时，未生成转写。") from None
