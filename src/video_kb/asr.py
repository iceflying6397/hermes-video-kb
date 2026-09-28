"""Offline local ASR using existing weights and a trusted Python runtime.

No cloud fallback, lazy installation, model download, user command template or
Hermes hooks. Only text and segment coverage return from the isolated worker.
"""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


class ASRUnavailable(ValueError):
    pass


REQUIRED_MODEL_FILES = ("model.bin", "config.json", "tokenizer.json")
MAX_MODEL_BYTES = 600 * 1024 * 1024
ASR_SECONDS = 300


def cached_model(config: dict) -> Path | None:
    candidates = []
    if config.get("local_asr_model"):
        candidates.append(Path(config["local_asr_model"]).expanduser())
    else:
        roots = [Path.home() / ".cache" / "huggingface" / "hub"]
        if os.environ.get("HF_HUB_CACHE"):
            roots.insert(0, Path(os.environ["HF_HUB_CACHE"]).expanduser())
        if os.environ.get("HF_HOME"):
            roots.insert(0, Path(os.environ["HF_HOME"]).expanduser() / "hub")
        for size in ("small", "base", "tiny"):
            for root in roots:
                folder = root / ("models--Systran--faster-whisper-" + size) / "snapshots"
                if folder.is_dir():
                    candidates.extend(sorted(folder.iterdir()))
    for path in candidates:
        try:
            if (all((path / f).is_file() for f in REQUIRED_MODEL_FILES)
                    and 0 < (path / "model.bin").stat().st_size <= MAX_MODEL_BYTES):
                return path.resolve()
        except OSError:
            continue
    return None


def _runtime(config: dict) -> Path | None:
    if importlib.util.find_spec("faster_whisper"):
        return Path(sys.executable)
    from .providers import _hermes_install
    try:
        _, binary = _hermes_install(config)
        check = subprocess.run([str(binary), "-I", "-c", "import importlib.util;raise SystemExit(0 if importlib.util.find_spec('faster_whisper') else 1)"],
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15, check=False)
        return binary if check.returncode == 0 else None
    except Exception:
        return None


def check_asr(*, config: dict | None = None) -> dict:
    """Read only non-secret capability; never call a model or install anything."""
    import shutil
    config = config or {}
    runtime = _runtime(config)
    model = cached_model(config)
    decoder = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
    available = bool(runtime and model and decoder)
    missing = []
    if not runtime:
        missing.append("本地 faster-whisper 运行组件")
    if not model:
        missing.append("已有 small/base/tiny 本地识别权重")
    if not decoder:
        missing.append("ffmpeg/ffprobe 音轨处理组件")
    return {"available": available, "message": "可使用现有本地模型；音频不上传，不自动下载。实际识别效果尚需样例验证。" if available else "缺少" + "、".join(missing) + "；不会自动下载或改用云端服务。", "backend": "faster-whisper" if runtime else None, "model_cached": bool(model), "decoder_available": decoder}


def transcribe_audio(audio: Path, *, duration: float, config: dict | None = None) -> dict:
    from .media import local_environment
    config = config or {}
    model, runtime = cached_model(config), _runtime(config)
    if not model or not runtime:
        raise ASRUnavailable(check_asr(config=config)["message"])
    if audio.is_symlink() or not audio.is_file() or audio.stat().st_size > 40 * 1024 * 1024:
        raise ASRUnavailable("本地音轨格式或大小异常。")
    body = json.dumps({"audio": str(audio.resolve()), "model": str(model), "duration": duration}).encode()
    args = [str(runtime), "-I", "-B", str(Path(__file__).resolve()), "--offline-worker"]
    try:
        # A file-backed result and worker RLIMIT_FSIZE prevent unbounded output allocation.
        with tempfile.TemporaryFile(dir=audio.parent) as output:
            result = subprocess.run(args, input=body, stdout=output, stderr=subprocess.DEVNULL, env=local_environment(), timeout=ASR_SECONDS + 10, check=False)
            if result.returncode or output.tell() > 500000:
                raise ValueError()
            output.seek(0)
            data = json.load(output)
        if not isinstance(data, dict) or not isinstance(data.get("text"), str) or not data["text"].strip() or len(data["text"]) > 50000:
            raise ValueError()
        return data
    except (OSError, ValueError, subprocess.SubprocessError):
        raise ASRUnavailable("本地语音识别失败或超过 5 分钟，任务已保留；没有上传音频或切换收费服务。") from None


def _worker():
    import resource
    resource.setrlimit(resource.RLIMIT_CPU, (ASR_SECONDS, ASR_SECONDS))
    resource.setrlimit(resource.RLIMIT_FSIZE, (500000, 500000))
    # Python network APIs are denied as an extra guard; this is not an OS sandbox.
    def no_network(event, args):
        if event in {"socket.connect", "socket.getaddrinfo", "subprocess.Popen", "os.system"}:
            raise RuntimeError("offline worker")
    sys.addaudithook(no_network)
    from faster_whisper import WhisperModel
    request = json.loads(sys.stdin.buffer.read(10000))
    model = WhisperModel(request["model"], device="cpu", compute_type="int8", cpu_threads=2, num_workers=1, local_files_only=True)
    segments, info = model.transcribe(request["audio"], beam_size=5, condition_on_previous_text=False, vad_filter=True)
    lines, count, first, last, truncated = [], 0, None, 0.0, False
    for segment in segments:
        # Match Hermes's silence suppression principles without its lazy install/cloud dispatch.
        if segment.no_speech_prob > 0.6 and segment.avg_logprob < -1.0:
            continue
        value = segment.text.strip()
        if not value:
            continue
        first = float(segment.start) if first is None else first
        last = float(segment.end)
        count += len(value) + 1
        if count > 50000:
            truncated = True
            break
        lines.append(value)
    actual_duration = float(info.duration)
    duration_mismatch = abs(actual_duration - float(request["duration"])) > max(2.0, float(request["duration"]) * 0.05)
    print(json.dumps({"text": "\n".join(lines), "first_speech_seconds": first, "last_speech_seconds": last,
                      "audio_seconds": actual_duration, "media_seconds": float(request["duration"]), "duration_mismatch": duration_mismatch,
                      "truncated": truncated, "backend": "faster-whisper-local", "model": Path(request["model"]).parent.parent.name}, ensure_ascii=False))


if __name__ == "__main__" and sys.argv[1:] == ["--offline-worker"]:
    try:
        _worker()
    except Exception:
        raise SystemExit(1) from None
