"""FFmpeg 媒体层测试（自动生成测试视频）。"""

import shutil
import subprocess

import pytest

from taggycat.media.ffmpeg import (
    FFmpegError,
    detect_scenes,
    extract_audio,
    extract_frames,
    extract_frames_by_scene,
    parse_progress_line,
    probe,
)

HAS_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))

pytestmark = pytest.mark.skipif(not HAS_FFMPEG, reason="需要 ffmpeg/ffprobe")


@pytest.fixture(scope="module")
def sample_video(tmp_path_factory) -> str:
    """生成 6 秒测试视频（两段场景 + 正弦波音轨）。"""
    path = tmp_path_factory.mktemp("media") / "sample.mp4"
    # 前 3 秒 testsrc，后 3 秒 mandelbrot（制造场景切换）
    cmd = [
        "ffmpeg", "-hide_banner", "-y",
        "-f", "lavfi", "-i", "testsrc=duration=3:size=320x240:rate=10",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=6",
        "-f", "lavfi", "-i", "mandelbrot=start_duration=3:duration=3:size=320x240:rate=10",
        "-filter_complex",
        "[0:v][2:v]concat=n=2:v=1:a=0[v];[v][1:v]merge=shortest=0[vout]",
        "-map", "[vout]", "-map", "1:a",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-shortest",
        str(path),
    ]
    # merge filter 不一定存在，回退到简单方案
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        cmd = [
            "ffmpeg", "-hide_banner", "-y",
            "-f", "lavfi", "-i", "testsrc=duration=6:size=320x240:rate=10",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=6",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            "-c:a", "aac",
            str(path),
        ]
        subprocess.run(cmd, check=True, capture_output=True)
    return str(path)


def test_probe(sample_video):
    info = probe(sample_video)
    assert info.duration == pytest.approx(6.0, abs=0.3)
    assert info.width == 320
    assert info.height == 240
    assert info.has_audio
    assert info.video_codec == "h264"
    assert info.aspect in ("4:3", "8:6")


def test_probe_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        probe(tmp_path / "nope.mp4")


def test_extract_frames(sample_video, tmp_path):
    frames = extract_frames(sample_video, tmp_path / "f", interval=2.0)
    assert len(frames) >= 2
    assert all(f.stat().st_size > 0 for f in frames)
    # 时间顺序
    assert frames == sorted(frames)


def test_detect_scenes(sample_video):
    cuts = detect_scenes(sample_video, threshold=0.3)
    assert isinstance(cuts, list)
    # 无缝视频可能 0 切点（回退为起点），有则时间应落在时长内
    for c in cuts:
        assert 0.0 <= c.time_sec <= 6.5


def test_scene_frames(sample_video, tmp_path):
    frames = extract_frames_by_scene(sample_video, tmp_path / "s", threshold=0.25)
    assert len(frames) >= 1
    for p, t in frames:
        assert p.exists()
        assert 0.0 <= t <= 6.5


def test_extract_audio(sample_video, tmp_path):
    out = extract_audio(sample_video, tmp_path / "a.wav")
    assert out.exists()
    assert out.stat().st_size > 1000


def test_progress_parser():
    assert parse_progress_line("out_time_us=1500000") == pytest.approx(1.5)
    assert parse_progress_line("out_time_ms=2000000") == pytest.approx(2.0)
    assert parse_progress_line("frame=10") is None
    assert parse_progress_line("speed=2x") is None


def test_ffmpeg_error_type(tmp_path):
    # 文件不存在 -> FileNotFoundError
    with pytest.raises(FileNotFoundError):
        probe(tmp_path / "does_not_exist.mp4")
    # 非媒体文件 -> FFmpegError（ffprobe 解析失败）
    bad = tmp_path / "bad.mp4"
    bad.write_bytes(b"not a media file at all")
    with pytest.raises(FFmpegError):
        probe(bad)
