"""讲解配音与原声的基频（音高）估计与对齐。

流程：逐段估计配音基频与对应原声片段基频 → 任务级中位数得半音差 →
ffmpeg `asetrate + aresample + atempo` 移调配音（时长不变）。
"""

import math
import subprocess
from pathlib import Path

import loguru

logger = loguru.logger

# 估计参数
_SR = 8000            # 分析采样率（人声基频足够）
_FRAME = 0.30         # 分析窗长（秒）
_HOP = 0.15           # 滑窗步长（秒）
_F0_LO = 70.0         # 人声基频下界（男低音）
_F0_HI = 400.0        # 人声基频上界（女高音）
_PEAK_RATIO = 0.4     # 自相关峰阈值（相对零延迟能量）
_CONSIST_FRAC = 0.5   # 至少该比例的帧与中位数相差 ≤1 半音才采信（过滤音乐/噪声）


def decode_mono(path, start: float | None = None,
                dur: float | None = None, sr: int = _SR):
    """ffmpeg 解码为单声道 float32 PCM（np.ndarray）；失败返回 None。"""
    try:
        import numpy as np
    except ImportError:
        return None
    from taggycat.media.ffmpeg import ffmpeg_bin

    cmd = [ffmpeg_bin(), "-hide_banner", "-nostdin", "-v", "error"]
    if start is not None:
        cmd += ["-ss", f"{max(0.0, start):.3f}"]
    if dur is not None:
        cmd += ["-t", f"{max(0.1, dur):.3f}"]
    cmd += ["-i", str(path), "-f", "s16le", "-ac", "1", "-ar", str(sr), "-"]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0 or not r.stdout:
        return None
    x = np.frombuffer(r.stdout, dtype="<i2").astype(np.float32) / 32768.0
    return x if x.size else None


def estimate_f0(samples, sr: int = _SR) -> float | None:
    """滑窗自相关估计基频（Hz）；无清晰人声周期返回 None。"""
    try:
        import numpy as np
    except ImportError:
        return None

    win = int(_FRAME * sr)
    hop = int(_HOP * sr)
    if samples is None or samples.size < win:
        return None
    lag_min = max(2, int(sr / _F0_HI))
    lag_max = min(win - 1, int(sr / _F0_LO))
    if lag_max <= lag_min:
        return None
    nfft = 1 << (2 * win - 1).bit_length()      # ≥ 2*win-1，线性相关
    ests: list[float] = []
    for i in range(0, samples.size - win + 1, hop):
        x = samples[i:i + win].astype(np.float64)
        if float(np.mean(x * x)) < 1e-6:        # 静音帧
            continue
        x = x - x.mean()
        spec = np.fft.rfft(x, nfft)
        ac = np.fft.irfft(spec * np.conj(spec), nfft)[:win]
        if ac[0] <= 0:
            continue
        seg = ac[lag_min:lag_max]
        k = int(np.argmax(seg)) + lag_min
        if ac[k] < _PEAK_RATIO * ac[0]:         # 周期性不清晰（音乐/噪声）
            continue
        ests.append(sr / k)
    if not ests:
        return None
    med = float(np.median(ests))
    # 一致性：过半帧与中位数相差 ≤1 半音才可信；音乐/合唱/噪声逐帧发散 → 不可信
    close = sum(1 for e in ests if abs(12.0 * math.log2(e / med)) <= 1.0)
    if close < max(3, len(ests) * _CONSIST_FRAC):
        return None
    return med


def f0_of_audio(path, start: float | None = None,
                dur: float | None = None) -> float | None:
    """文件（或其中片段）的基频估计。"""
    x = decode_mono(path, start=start, dur=dur)
    return estimate_f0(x) if x is not None else None


def sample_rate_of(path) -> int:
    """ffprobe 读取音频流采样率（失败回退 44100）。"""
    from taggycat.media.ffmpeg import ffprobe_bin

    try:
        r = subprocess.run(
            [ffprobe_bin(), "-v", "error", "-select_streams", "a:0",
             "-show_entries", "stream=sample_rate", "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=30,
        )
        return int(r.stdout.strip().splitlines()[0])
    except (OSError, subprocess.TimeoutExpired, ValueError, IndexError):
        return 44100


_HAS_RUBBERBAND: bool | None = None


def _has_rubberband() -> bool:
    """检测当前 ffmpeg 是否带 librubberband（共振峰保持移调，音质远优于 asetrate）。"""
    global _HAS_RUBBERBAND
    if _HAS_RUBBERBAND is None:
        try:
            from taggycat.media.ffmpeg import ffmpeg_bin
            r = subprocess.run([ffmpeg_bin(), "-hide_banner", "-filters"],
                               capture_output=True, text=True, timeout=30)
            _HAS_RUBBERBAND = "rubberband" in r.stdout
        except (OSError, subprocess.TimeoutExpired):
            _HAS_RUBBERBAND = False
    return _HAS_RUBBERBAND


def apply_pitch(src, semitones: float) -> Path | None:
    """移调音频 semitones 个半音（时长不变），输出同目录 wav；失败返回 None。

    优先使用 rubberband（保持共振峰，人声不变声）；
    无该滤镜时回退 asetrate+atempo（音质较差，仅兜底）。
    """
    from taggycat.media.ffmpeg import ffmpeg_bin

    src = Path(src)
    out = src.with_name(f"{src.stem}_p{semitones:+.1f}.wav")
    rate = sample_rate_of(src)
    r = 2.0 ** (semitones / 12.0)
    if _has_rubberband():
        af = f"rubberband=pitch={r:.8f}"
    else:
        af = f"asetrate={rate}*{r:.8f},aresample={rate},atempo={1.0 / r:.8f}"
    try:
        subprocess.run(
            [ffmpeg_bin(), "-hide_banner", "-nostdin", "-v", "error", "-y",
             "-i", str(src), "-af", af, "-c:a", "pcm_s16le", str(out)],
            capture_output=True, timeout=120, check=True,
        )
    except (OSError, subprocess.TimeoutExpired, subprocess.CalledProcessError):
        return None
    return out if out.exists() and out.stat().st_size > 0 else None


def semitones_between(f_orig: float | None, f_voice: float | None) -> float | None:
    """原声相对配音的音高差（半音；正=配音偏低需升调）。"""
    if not f_orig or not f_voice or f_orig <= 0 or f_voice <= 0:
        return None
    return 12.0 * math.log2(f_orig / f_voice)
