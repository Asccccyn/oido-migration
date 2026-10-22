"""隔离子进程解码：ffmpeg/ffprobe 管道进出，不落盘临时文件（05 基础补完——韵律主链前置）。

对应施工包条目：
- 媒体探测由 ffprobe 真实取得格式、时长、采样率、声道、codec；
- ffmpeg 解码参数固定（pcm_f32le/16k/mono），子进程有时长限额与超时，
  取消/超限终止整个进程组，解码炸弹与畸形媒体失败可见（reason_code）；
- 立体声 downmix 用 ffmpeg 默认 -ac 1（均值）：相消风险不掩盖，
  在 transform_params.downmix_note 里如实标注，交上游标记可比性限制。

管道 stdin 输入不可 seek，ffprobe 对 WAV 这类头信息不足的容器给不出
format.duration——duration_s 记 NaN 表示未知，绝不伪造 0；真实时长由
解码后的样本数取得（duration_s 字段在 DecodeResult 上）。
"""

from __future__ import annotations

import json
import math
import os
import selectors
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

import numpy as np

from oido.domain.states import require_reason

FFMPEG = "ffmpeg"
FFPROBE = "ffprobe"

# 解码参数固定（05：ffmpeg 解码参数固定；每个分析变体记录解码/重采样/声道策略）
DECODE_PROFILE = "pcm_f32le_16000_mono"
TARGET_SR = 16000
FFMPEG_ARGS = [
    FFMPEG, "-v", "error",
    "-i", "pipe:0",
    "-map", "0:a:0",
    "-ac", "1",            # ffmpeg 默认单声道 downmix（通道均值）
    "-ar", str(TARGET_SR),
    "-f", "f32le",
    "-",                   # stdout，不写临时文件
]
FFMPEG_VERSION_HINT = "9.x"   # 环境为 ffmpeg 9.0.2；仅作提示不作依据
DOWNMIX_NOTE = "mean_of_channels; phase-cancellation not compensated"


class DecodeError(Exception):
    """解码/探测失败。reason_code 必须取自 oido.domain.states.REASON_CODES。"""

    def __init__(self, reason_code: str, message: str):
        require_reason(reason_code)
        super().__init__(f"[{reason_code}] {message}")
        self.reason_code = reason_code
        self.message = message


@dataclass(frozen=True)
class MediaInfo:
    container: str
    duration_s: float          # 未知记 NaN（管道输入不可 seek 时 ffprobe 给不出）
    sample_rate_hz: int
    channels: int
    codec: str
    decode_profile: str = DECODE_PROFILE


@dataclass(frozen=True)
class DecodeResult:
    samples: np.ndarray        # float32 单声道 TARGET_SR
    sample_rate_hz: int
    timebase_id: str
    duration_s: float          # 真实时长 = 解码样本数 / 16000
    channels_source: int
    decode_profile: str
    transform_params: dict = field(default_factory=dict)
    warning: Optional[str] = None


def _kill_process_group(proc: subprocess.Popen) -> None:
    """杀掉整个进程组（start_new_session=True 使 pid==pgid），再收尸。"""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except OSError:
            pass
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def probe(data: bytes, *, timeout_s: float = 30.0) -> MediaInfo:
    """ffprobe 管道探测（stdin 喂字节，不写临时文件）。

    解析失败 / 无音频流 → DecodeError("invalid_media")；
    ffprobe 超时 → DecodeError("deadline_exceeded")（进程被终止）。
    """
    if not data:
        raise DecodeError("invalid_media", "empty input bytes")
    try:
        proc = subprocess.run(
            [
                FFPROBE, "-v", "error", "-print_format", "json",
                "-show_format", "-show_streams", "-",
            ],
            input=bytes(data),
            capture_output=True,
            timeout=timeout_s,
            start_new_session=True,
        )
    except subprocess.TimeoutExpired:
        raise DecodeError("deadline_exceeded", f"ffprobe exceeded {timeout_s}s")
    if proc.returncode != 0:
        tail = proc.stderr.decode("utf-8", errors="replace").strip()[-300:]
        raise DecodeError("invalid_media", f"ffprobe failed rc={proc.returncode}: {tail}")
    try:
        info = json.loads(proc.stdout.decode("utf-8", errors="strict"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise DecodeError("invalid_media", f"ffprobe json unparseable: {exc}")
    streams = info.get("streams") or []
    audio = [s for s in streams if s.get("codec_type") == "audio"]
    if not audio:
        raise DecodeError("invalid_media", "no audio stream")
    fmt = info.get("format") or {}
    container = str(fmt.get("format_name") or "unknown")
    stream = audio[0]
    try:
        sr = int(stream["sample_rate"])
        channels = int(stream["channels"])
    except (KeyError, TypeError, ValueError) as exc:
        raise DecodeError("invalid_media", f"audio stream missing sr/channels: {exc}")
    if sr <= 0 or channels <= 0:
        raise DecodeError("invalid_media", f"non-positive sr/channels: sr={sr} ch={channels}")
    # duration：优先 format 段，退回 stream 段；都没有（管道 WAV 常见）→ NaN 不伪造
    duration_s = float("nan")
    for holder in (fmt, stream):
        raw = holder.get("duration")
        if raw is None:
            continue
        try:
            candidate = float(raw)
        except (TypeError, ValueError):
            continue
        if candidate > 0:
            duration_s = candidate
            break
        raise DecodeError("invalid_media", f"non-positive duration {raw!r}")
    codec = str(stream.get("codec_name") or "unknown")
    return MediaInfo(
        container=container,
        duration_s=duration_s,
        sample_rate_hz=sr,
        channels=channels,
        codec=codec,
    )


def decode_mono_pcm(
    data: bytes,
    *,
    max_input_bytes: int = 60 * 1024 * 1024,
    max_seconds: float = 600.0,
    timeout_s: float = 120.0,
) -> DecodeResult:
    """解码为 16 kHz 单声道 f32le，全程 stdin/stdout 管道，不落盘。

    限额（05：子进程有时长上限，取消终止进程组）：
    - 输入字节 > max_input_bytes → resource_exhausted（不启动子进程）；
    - probe 已知时长 > max_seconds → resource_exhausted（预先拒绝）；
    - probe 时长未知（NaN）时，按解码输出字节流上限兜底执行：
      16k*f32*mono = 64000 B/s，超限杀进程组 → resource_exhausted；
    - 墙钟超 timeout_s → 杀进程组 → deadline_exceeded。
    """
    if not isinstance(data, (bytes, bytearray)):
        raise DecodeError("invalid_media", f"expected bytes input, got {type(data).__name__}")
    if len(data) > max_input_bytes:
        raise DecodeError(
            "resource_exhausted",
            f"input {len(data)} bytes exceeds max_input_bytes={max_input_bytes}",
        )
    if max_seconds <= 0 or timeout_s <= 0:
        raise DecodeError("resource_exhausted", "max_seconds/timeout_s must be positive")

    info = probe(data)

    if not math.isnan(info.duration_s) and info.duration_s > max_seconds:
        raise DecodeError(
            "resource_exhausted",
            f"probed duration {info.duration_s:.1f}s exceeds max_seconds={max_seconds}s",
        )

    proc = subprocess.Popen(
        FFMPEG_ARGS,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,   # 独立进程组：超时/超限整组 kill
    )

    def _feed(stdin) -> None:
        try:
            stdin.write(bytes(data))
        except (BrokenPipeError, OSError):
            pass
        finally:
            try:
                stdin.close()
            except OSError:
                pass

    stderr_buf = bytearray()

    def _drain_stderr(stderr) -> None:
        try:
            while True:
                chunk = stderr.read(65536)
                if not chunk:
                    break
                stderr_buf.extend(chunk)
        except (OSError, ValueError):
            pass

    writer = threading.Thread(target=_feed, args=(proc.stdin,), daemon=True)
    err_reader = threading.Thread(target=_drain_stderr, args=(proc.stderr,), daemon=True)
    writer.start()
    err_reader.start()

    # 输出上限：max_seconds 秒的 f32le/16k/mono 字节数（+缓冲余量）。
    # 这是对 probe 时长未知输入的流式解码炸弹防线。
    out_cap_bytes = int(max_seconds * TARGET_SR * 4) + 4096
    out = bytearray()
    state = "done"
    sel = selectors.DefaultSelector()
    sel.register(proc.stdout, selectors.EVENT_READ)
    try:
        deadline = time.monotonic() + timeout_s
        while True:
            remain = deadline - time.monotonic()
            if remain <= 0:
                state = "timeout"
                break
            events = sel.select(timeout=min(remain, 0.25))
            if not events:
                if proc.poll() is not None:
                    # 进程已退：把管道余量读干到 EOF
                    while True:
                        chunk = proc.stdout.read(1 << 20)
                        if not chunk:
                            break
                        out.extend(chunk)
                        if len(out) > out_cap_bytes:
                            state = "cap"
                            break
                    break
                continue
            chunk = proc.stdout.read1(1 << 20)
            if not chunk:
                break   # EOF：正常结束
            out.extend(chunk)
            if len(out) > out_cap_bytes:
                state = "cap"
                break
    finally:
        sel.close()

    if state == "timeout":
        _kill_process_group(proc)
        writer.join(timeout=5)
        err_reader.join(timeout=5)
        raise DecodeError("deadline_exceeded", f"ffmpeg exceeded timeout_s={timeout_s}s; process group killed")
    if state == "cap":
        _kill_process_group(proc)
        writer.join(timeout=5)
        err_reader.join(timeout=5)
        raise DecodeError(
            "resource_exhausted",
            f"decoded output exceeded max_seconds={max_seconds}s cap; process group killed",
        )

    try:
        rc = proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        _kill_process_group(proc)
        raise DecodeError("deadline_exceeded", "ffmpeg did not exit after stdout EOF")
    writer.join(timeout=5)
    err_reader.join(timeout=5)
    stderr_text = bytes(stderr_buf).decode("utf-8", errors="replace").strip()

    if rc != 0:
        raise DecodeError("invalid_media", f"ffmpeg rc={rc}: {stderr_text[-300:]}")
    warning = stderr_text if stderr_text else None

    if not out:
        raise DecodeError("invalid_media", "ffmpeg produced no audio samples")
    samples = np.frombuffer(bytes(out), dtype="<f4").astype(np.float32)
    if not np.isfinite(samples).all():
        raise DecodeError("invalid_media", "non-finite samples in decoded audio")

    duration_s = samples.size / TARGET_SR   # 真实时长：解码后样本数
    transform_params = {
        "ffmpeg_args": list(FFMPEG_ARGS),
        "ffmpeg_version_hint": FFMPEG_VERSION_HINT,
        "source_channels": info.channels,
        "source_sr": info.sample_rate_hz,
        "downmix": "ffmpeg_pan_mono",
        "downmix_note": DOWNMIX_NOTE,
    }
    return DecodeResult(
        samples=samples,
        sample_rate_hz=TARGET_SR,
        timebase_id="tb_16k_mono",
        duration_s=duration_s,
        channels_source=info.channels,
        decode_profile=DECODE_PROFILE,
        transform_params=transform_params,
        warning=warning,
    )
