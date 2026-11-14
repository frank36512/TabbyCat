"""基频估计与讲解音高对齐（voice_pitch_match）测试。"""

import shutil
import subprocess
from types import SimpleNamespace

import pytest

from taggycat.media.ffmpeg import ffmpeg_bin, media_duration

HAS_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
pytestmark = pytest.mark.skipif(not HAS_FFMPEG, reason="需要 ffmpeg/ffprobe")
np = pytest.importorskip("numpy", reason="音高估计依赖 numpy")


def _sine(path, freq: float, dur: float = 1.0):
    subprocess.run(
        [ffmpeg_bin(), "-hide_banner", "-y", "-f", "lavfi",
         "-i", f"sine=frequency={freq}:duration={dur}",
         "-ar", "44100", "-ac", "1", str(path)],
        check=True, capture_output=True,
    )
    return path


def test_estimate_f0_on_sine(tmp_path):
    from taggycat.media.pitch import f0_of_audio

    f = f0_of_audio(_sine(tmp_path / "a.wav", 220))
    assert f is not None
    assert abs(f - 220) / 220 < 0.05


def test_estimate_f0_silence_none(tmp_path):
    from taggycat.media.pitch import f0_of_audio

    p = tmp_path / "sil.wav"
    subprocess.run(
        [ffmpeg_bin(), "-hide_banner", "-y", "-f", "lavfi",
         "-i", "anullsrc=r=8000:cl=mono:d=0.5", str(p)],
        check=True, capture_output=True,
    )
    assert f0_of_audio(p) is None


def test_semitones_between():
    from taggycat.media.pitch import semitones_between

    assert semitones_between(440, 440) == pytest.approx(0.0)
    assert semitones_between(880, 440) == pytest.approx(12.0)
    assert semitones_between(440, 880) == pytest.approx(-12.0)
    assert semitones_between(None, 440) is None
    assert semitones_between(0, 440) is None
    assert semitones_between(440, None) is None


def test_apply_pitch_up_keeps_duration(tmp_path):
    from taggycat.media.pitch import apply_pitch, f0_of_audio

    p = _sine(tmp_path / "v.wav", 220, 1.0)
    out = apply_pitch(p, 3.0)
    assert out is not None and out.name != p.name
    f = f0_of_audio(out)
    expect = 220 * (2 ** (3 / 12))          # ≈ 261.6 Hz
    assert f is not None
    assert abs(f - expect) / expect < 0.05
    assert abs(media_duration(out) - media_duration(p)) < 0.08


def test_apply_pitch_down(tmp_path):
    from taggycat.media.pitch import apply_pitch, f0_of_audio

    p = _sine(tmp_path / "v.wav", 300, 1.0)
    out = apply_pitch(p, -4.0)
    assert out is not None
    f = f0_of_audio(out)
    expect = 300 * (2 ** (-4 / 12))         # ≈ 238.1 Hz
    assert f is not None
    assert abs(f - expect) / expect < 0.05


def test_align_pitch_per_segment(tmp_path):
    """段级移调：各段按自己的原声基频分别移调（不再统一中位数）。"""
    from taggycat.config import OutputConfig
    from taggycat.models import Segment
    from taggycat.pipeline.stages_m4 import _align_pitch

    # 源音频：前 1s 247Hz、后 1s 300Hz（模拟不同段原声音高不同）
    a = _sine(tmp_path / "a.wav", 247.0, 1.0)
    b = _sine(tmp_path / "b.wav", 300.0, 1.0)
    src = tmp_path / "src.wav"
    subprocess.run(
        [ffmpeg_bin(), "-hide_banner", "-y", "-i", str(a), "-i", str(b),
         "-filter_complex", "[0:a][1:a]concat=n=2:v=0:a=1[a]", "-map", "[a]", str(src)],
        check=True, capture_output=True,
    )
    voice = _sine(tmp_path / "voice.wav", 220.0, 1.0)

    def mk_segs():
        return [
            # 原声 247Hz → 约 +2.0 半音
            Segment(segment_id="s001", source_id="m1", start=0.0, end=1.0,
                    audio_path=str(voice)),
            # 原声 300Hz → 约 +4.9 半音
            Segment(segment_id="s002", source_id="m1", start=1.0, end=2.0,
                    audio_path=str(voice)),
            Segment(segment_id="s003", source_id="m1", start=0.0, end=1.0,
                    keep_audio=True),          # 让路段不处理
        ]

    o = OutputConfig(voice_pitch_match=True, voice_pitch_max_semitones=8.0)
    ctx = SimpleNamespace(
        config=SimpleNamespace(output=o),
        state=SimpleNamespace(
            sources=[SimpleNamespace(media_id="m1", path=str(src))]),
    )

    segs = mk_segs()
    applied = _align_pitch(ctx, segs)
    assert len(applied) == 2
    assert abs(applied[0] - 2.0) < 0.6
    assert abs(applied[1] - 4.9) < 0.6
    # 各段移调后 f0 逼近自己的原声
    from taggycat.media.pitch import f0_of_audio

    f1 = f0_of_audio(segs[0].audio_path)
    f2 = f0_of_audio(segs[1].audio_path)
    assert f1 and abs(f1 - 247) / 247 < 0.08
    assert f2 and abs(f2 - 300) / 300 < 0.08
    assert segs[0].voice_duration > 0          # 时长元数据同步
    assert not segs[2].audio_path              # 让路段未被动

    # 钳制：各段独立限制在 ±1 内
    o.voice_pitch_max_semitones = 1.0
    segs = mk_segs()
    applied = _align_pitch(ctx, segs)
    assert applied and all(abs(x) <= 1.0 for x in applied)

    # 开关关闭
    o.voice_pitch_match = False
    assert _align_pitch(ctx, mk_segs()) == []
