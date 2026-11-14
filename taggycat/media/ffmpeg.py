"""FFmpeg 媒体工具层：探测、抽帧、场景切分、字幕/音频提取、进度解析。"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

from loguru import logger

from taggycat.models import MediaInfo, SceneCut, SubtitleStreamInfo


class FFmpegError(RuntimeError):
    def __init__(self, msg: str, cmd: list[str] | None = None, stderr: str = ""):
        super().__init__(msg)
        self.cmd = cmd or []
        self.stderr = stderr


def encode_args(o) -> list[str]:
    """统一的视频/音频编码参数（传入 OutputConfig）。"""
    return [
        "-c:v", o.video_codec, "-preset", o.preset,
        "-crf", str(o.crf), "-pix_fmt", "yuv420p",
        "-c:a", o.audio_codec, "-b:a", o.audio_bitrate,
    ]


def load_probe(path: str | Path) -> MediaInfo | None:
    """读取 probe 阶段落盘的 MediaInfo；文件缺失或损坏返回 None。"""
    p = Path(path)
    if not p.exists():
        return None
    try:
        return MediaInfo.model_validate_json(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def ffmpeg_bin() -> str:
    exe = shutil.which("ffmpeg")
    if not exe:
        raise FFmpegError("未找到 ffmpeg，请确认已安装并加入 PATH")
    return exe


def ffprobe_bin() -> str:
    exe = shutil.which("ffprobe")
    if not exe:
        raise FFmpegError("未找到 ffprobe，请确认已安装并加入 PATH")
    return exe


# ---------------------------------------------------------------------
# 进度解析
# ---------------------------------------------------------------------
_TIME_RE = re.compile(r"^out_time_ms=(\d+)$")
_TIME_RE_US = re.compile(r"^out_time_us=(\d+)$")


def parse_progress_line(line: str) -> float | None:
    """从 `-progress pipe:1` 输出行解析已处理秒数。

    注意：ffmpeg 的 out_time_ms 实际单位是微秒（历史 quirk），
    优先使用 out_time_us，其次 out_time_ms，两者均按微秒处理。
    """
    line = line.strip()
    m = _TIME_RE_US.match(line) or _TIME_RE.match(line)
    if m:
        return int(m.group(1)) / 1_000_000.0
    return None


# ---------------------------------------------------------------------
# 运行中进程注册表（软件退出时强杀）
# ---------------------------------------------------------------------
_RUNNING: set[subprocess.Popen] = set()


def kill_running() -> int:
    """强杀所有运行中的外部命令进程，返回击杀数量（进程可能已退出）。"""
    n = 0
    for proc in list(_RUNNING):
        try:
            proc.kill()
            n += 1
        except OSError:
            pass
    return n


def run_command(
    cmd: list[str],
    *,
    total_duration: float = 0.0,
    on_progress: Callable[[float], None] | None = None,
    timeout: int | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess:
    """执行外部命令；on_progress 收到已处理秒数（若命令支持 -progress）。"""
    logger.debug("执行: {}", " ".join(cmd))
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    _RUNNING.add(proc)
    try:
        assert proc.stdout is not None
        if on_progress:
            for line in proc.stdout:
                sec = parse_progress_line(line)
                if sec is not None:
                    on_progress(sec)
        try:
            _, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired as e:
            proc.kill()
            proc.communicate()
            raise FFmpegError(f"命令超时 ({timeout}s): {cmd[0]}", cmd=cmd) from e

        if check and proc.returncode != 0:
            tail = "\n".join((stderr or "").strip().splitlines()[-20:])
            raise FFmpegError(
                f"命令失败 (exit={proc.returncode}): {tail}",
                cmd=cmd, stderr=stderr or "",
            )
        return subprocess.CompletedProcess(cmd, proc.returncode, "", stderr or "")
    finally:
        _RUNNING.discard(proc)


# ---------------------------------------------------------------------
# 探测
# ---------------------------------------------------------------------
def _parse_fps(raw: str) -> float:
    if not raw or raw in ("0/0", "N/A"):
        return 0.0
    if "/" in raw:
        num, _, den = raw.partition("/")
        try:
            d = float(den)
            return float(num) / d if d else 0.0
        except ValueError:
            return 0.0
    try:
        return float(raw)
    except ValueError:
        return 0.0


def probe(path: str | Path) -> MediaInfo:
    """ffprobe 获取媒体信息。"""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"文件不存在: {path}")

    cmd = [
        ffprobe_bin(),
        "-v", "error",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]
    out = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if out.returncode != 0:
        raise FFmpegError(f"ffprobe 失败: {out.stderr.strip()}", cmd=cmd, stderr=out.stderr)

    data = json.loads(out.stdout or "{}")
    fmt = data.get("format", {})
    streams = data.get("streams", [])

    info = MediaInfo(path=str(path.resolve()))
    info.duration = float(fmt.get("duration") or 0.0)
    info.size_bytes = int(fmt.get("size") or 0) or path.stat().st_size
    info.format_name = fmt.get("format_name", "")
    if fmt.get("bit_rate"):
        info.bitrate_kbps = int(int(fmt["bit_rate"]) / 1000)

    for s in streams:
        ctype = s.get("codec_type")
        if ctype == "video" and not info.width:
            info.width = int(s.get("width") or 0)
            info.height = int(s.get("height") or 0)
            info.video_codec = s.get("codec_name", "")
            info.fps = _parse_fps(s.get("avg_frame_rate") or s.get("r_frame_rate") or "")
            if not info.duration and s.get("duration"):
                info.duration = float(s["duration"])
        elif ctype == "audio" and not info.has_audio:
            info.has_audio = True
            info.audio_codec = s.get("codec_name", "")
        elif ctype == "subtitle":
            info.subtitle_streams.append(
                SubtitleStreamInfo(
                    index=int(s.get("index", 0)),
                    codec=s.get("codec_name", ""),
                    language=(s.get("tags") or {}).get("language", ""),
                    title=(s.get("tags") or {}).get("title", ""),
                )
            )
    return info


# ---------------------------------------------------------------------
# 抽帧
# ---------------------------------------------------------------------
def extract_frames(
    src: str | Path,
    out_dir: str | Path,
    *,
    interval: float = 10.0,
    max_frames: int = 0,
    scale_width: int = 0,
    quality: int = 3,
    on_progress: Callable[[float], None] | None = None,
    info: MediaInfo | None = None,
) -> list[Path]:
    """按固定间隔抽帧，返回帧文件列表（按时间排序）。

    info 可外部传入以避免重复 ffprobe。
    """
    src = Path(src)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    if info is None:
        info = probe(src)
    filters = [f"fps=1/{max(interval, 0.1):.4f}"]
    if scale_width > 0:
        filters.append(f"scale={scale_width}:-2")

    pattern = out_dir / "frame_%06d.jpg"
    cmd = [
        ffmpeg_bin(),
        "-hide_banner", "-y",
        "-i", str(src),
        "-vf", ",".join(filters),
        "-q:v", str(quality),
        "-progress", "pipe:1", "-nostats",
        str(pattern),
    ]
    if max_frames > 0:
        cmd.insert(-1, "-frames:v")
        cmd.insert(-1, str(max_frames))

    run_command(cmd, total_duration=info.duration, on_progress=on_progress)
    frames = sorted(out_dir.glob("frame_*.jpg"))
    logger.info("抽帧完成: {} -> {} 帧", src.name, len(frames))
    return frames


def extract_frames_by_scene(
    src: str | Path,
    out_dir: str | Path,
    *,
    threshold: float = 0.30,
    scale_width: int = 0,
    quality: int = 3,
    min_gap: float = 1.0,
    cuts: list[SceneCut] | None = None,
) -> list[tuple[Path, float]]:
    """场景切分后，取每个切点后第一帧。返回 (帧路径, 时间秒)。

    cuts 可外部传入以避免重复检测（场景检测对长片较耗时）。
    """
    src = Path(src)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    if cuts is None:
        cuts = detect_scenes(src, threshold=threshold)
    if not cuts:
        cuts = [SceneCut(time_sec=0.0, score=1.0)]

    results: list[tuple[Path, float]] = []
    prev = -min_gap
    for i, cut in enumerate(cuts):
        if cut.time_sec - prev < min_gap and i != 0:
            continue
        prev = cut.time_sec
        out = out_dir / f"scene_{i:04d}_{cut.time_sec:09.2f}.jpg"
        vf = "scale=640:-2" if not scale_width else f"scale={scale_width}:-2"
        cmd = [
            ffmpeg_bin(), "-hide_banner", "-y",
            "-ss", f"{cut.time_sec:.3f}",
            "-i", str(src),
            "-frames:v", "1",
            "-vf", vf,
            "-q:v", str(quality),
            str(out),
        ]
        try:
            run_command(cmd, timeout=60)
            results.append((out, cut.time_sec))
        except FFmpegError as e:
            logger.warning("抽帧失败 @ {:.2f}s: {}", cut.time_sec, e)
    logger.info("场景抽帧完成: {} -> {} 帧", src.name, len(results))
    return results


def _parse_scene_cuts(stderr: str) -> list[SceneCut]:
    """解析 metadata=print 输出中的 pts_time / scene_score 对。"""
    pts_re = re.compile(r"pts_time:(\d+(?:\.\d+)?)")
    score_re = re.compile(r"lavfi\.scene_score=([\d.]+)")
    cuts: list[SceneCut] = []
    cur_time: float | None = None
    for line in stderr.splitlines():
        m = pts_re.search(line)
        if m:
            cur_time = float(m.group(1))
        m2 = score_re.search(line)
        if m2 and cur_time is not None:
            cuts.append(SceneCut(time_sec=cur_time, score=float(m2.group(1))))
            cur_time = None
    return cuts


def detect_scenes(
    src: str | Path, *, threshold: float = 0.30, timeout: int | None = 1800,
    info: MediaInfo | None = None,
) -> list[SceneCut]:
    """ffmpeg scene 检测，返回切点列表。

    info 可外部传入以避免重复 ffprobe。
    """
    src = Path(src)
    if info is None:
        info = probe(src)
    # metadata=print 的切点信息输出到 stderr，无需 -progress
    cmd = [
        ffmpeg_bin(),
        "-hide_banner",
        "-i", str(src),
        "-vf", f"select='gt(scene,{threshold})',metadata=print",
        "-an", "-f", "null", "-",
    ]
    proc = run_command(cmd, timeout=timeout)
    cuts = _parse_scene_cuts(proc.stderr or "")
    logger.info("场景切分: {} -> {} 个切点 (threshold={})", src.name, len(cuts), threshold)
    if info.duration and not cuts:
        cuts = [SceneCut(time_sec=0.0, score=1.0)]
    return cuts


def _seek_one_frame(
    src: Path, t: float, out: Path, scale: int, quality: int
) -> None:
    """在指定时刻 seek 抽单帧（回退/补点路径）。"""
    cmd = [
        ffmpeg_bin(), "-hide_banner", "-y",
        "-ss", f"{t:.3f}", "-i", str(src),
        "-frames:v", "1", "-vf", f"scale={scale}:-2",
        "-q:v", str(quality), str(out),
    ]
    run_command(cmd, timeout=60)


def detect_scenes_with_frames(
    src: str | Path,
    out_dir: str | Path,
    *,
    threshold: float = 0.30,
    scale_width: int = 0,
    quality: int = 3,
    min_gap: float = 1.0,
    timeout: int | None = 1800,
    info: MediaInfo | None = None,
) -> tuple[list[SceneCut], list[tuple[Path, float]]]:
    """单条 ffmpeg 一次遍历产出场景切点与场景帧（≤2 个进程）。

    返回 (全部切点, 经 min_gap 过滤后的 (帧路径, 时间))。
    两路 select 帧数理论一致；不一致时回退逐点 seek 以保正确性。
    """
    src = Path(src)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    if info is None:
        info = probe(src)

    scale = scale_width or 640
    tmp_pat = out_dir / "_cut_%04d.jpg"
    fc = (
        f"[0:v]split=2[a][b];"
        f"[a]select='gt(scene,{threshold})',metadata=print[x];"
        f"[b]select='gt(scene,{threshold})',scale={scale}:-2[y]"
    )
    cmd = [
        ffmpeg_bin(), "-hide_banner", "-i", str(src),
        "-filter_complex", fc,
        "-map", "[x]", "-f", "null", "-",
        "-map", "[y]", "-fps_mode", "vfr", "-q:v", str(quality),
        str(tmp_pat),
    ]
    proc = run_command(cmd, timeout=timeout)
    cuts = _parse_scene_cuts(proc.stderr or "")
    raw_imgs = sorted(out_dir.glob("_cut_*.jpg"))
    aligned = len(raw_imgs) == len(cuts)
    if not aligned:
        logger.warning(
            "场景帧/切点数不一致（{} 帧 vs {} 切点），回退逐点抽帧",
            len(raw_imgs), len(cuts),
        )
        for p in raw_imgs:
            p.unlink(missing_ok=True)

    if not cuts and info.duration:
        cuts = [SceneCut(time_sec=0.0, score=1.0)]

    results: list[tuple[Path, float]] = []
    prev = -min_gap
    for i, cut in enumerate(cuts):
        out = out_dir / f"scene_{i:04d}_{cut.time_sec:09.2f}.jpg"
        if i != 0 and cut.time_sec - prev < min_gap:
            if aligned:
                raw_imgs[i].unlink(missing_ok=True)
            continue
        prev = cut.time_sec
        if aligned:
            raw_imgs[i].replace(out)
        else:
            _seek_one_frame(src, cut.time_sec, out, scale, quality)
        results.append((out, cut.time_sec))

    logger.info(
        "场景切分+抽帧: {} -> {} 个切点，{} 帧",
        src.name, len(cuts), len(results),
    )
    return cuts, results


# ---------------------------------------------------------------------
# 字幕 / 音频
# ---------------------------------------------------------------------
def extract_subtitle(
    src: str | Path,
    out_path: str | Path,
    *,
    stream_index: int | None = None,
    info: MediaInfo | None = None,
) -> Path:
    """提取内嵌字幕流为 srt（stream_index 为 ffprobe 的全局流序号）。"""
    src, out_path = Path(src), Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    if stream_index is None:
        if info is None:
            info = probe(src)
        if not info.subtitle_streams:
            raise FFmpegError(f"无内嵌字幕流: {src}")
        stream_index = info.subtitle_streams[0].index

    cmd = [
        ffmpeg_bin(), "-hide_banner", "-y",
        "-i", str(src),
        "-map", f"0:{stream_index}",
        "-c:s", "srt",
        str(out_path),
    ]
    run_command(cmd, timeout=300)
    if not out_path.exists() or out_path.stat().st_size == 0:
        raise FFmpegError(f"字幕提取失败: {src}")
    logger.info("字幕提取: {} -> {}", src.name, out_path.name)
    return out_path


def extract_audio(
    src: str | Path,
    out_path: str | Path,
    *,
    sample_rate: int = 16000,
    mono: bool = True,
    on_progress: Callable[[float], None] | None = None,
    info: MediaInfo | None = None,
) -> Path:
    """抽取音轨为 wav（ASR 输入）。

    info 可外部传入以避免重复 ffprobe。
    """
    src, out_path = Path(src), Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if info is None:
        info = probe(src)
    if not info.has_audio:
        raise FFmpegError(f"文件无音轨: {src}")

    cmd = [
        ffmpeg_bin(), "-hide_banner", "-y",
        "-i", str(src),
        "-vn",
        "-ac", "1" if mono else "2",
        "-ar", str(sample_rate),
        "-c:a", "pcm_s16le",
        "-progress", "pipe:1", "-nostats",
        str(out_path),
    ]
    run_command(cmd, total_duration=info.duration, on_progress=on_progress)
    logger.info("音轨提取: {} -> {}", src.name, out_path.name)
    return out_path


def media_duration(path: str | Path) -> float:
    return probe(path).duration
