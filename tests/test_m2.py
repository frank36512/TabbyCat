"""M2 测试：LLM JSON 抽取 / 时间线融合 / ASR+Understand 阶段。"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from taggycat.asr import Transcript, TranscriptItem
from taggycat.config import load_config
from taggycat.llm import extract_json
from taggycat.models import SceneCut, StageStatus
from taggycat.pipeline import build_pipeline
from taggycat.store import TaskStore
from taggycat.timeline import build_timeline

HAS_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------
# LLM JSON 抽取
# ---------------------------------------------------------------------
def test_extract_json_plain():
    assert extract_json('{"a": 1}') == {"a": 1}
    assert extract_json('[1, 2, 3]') == [1, 2, 3]


def test_extract_json_fenced():
    assert extract_json('```json\n{"x": "y"}\n```') == {"x": "y"}


def test_extract_json_with_prose():
    text = '好的，结果如下：\n{"t": 1.5, "desc": "雨夜街道"}\n希望有帮助'
    assert extract_json(text) == {"t": 1.5, "desc": "雨夜街道"}


def test_extract_json_empty_raises():
    from taggycat.llm import LLMApiError

    with pytest.raises(LLMApiError):
        extract_json("")
    with pytest.raises(LLMApiError):
        extract_json("完全不是 JSON 的文字")


# ---------------------------------------------------------------------
# 时间线
# ---------------------------------------------------------------------
def test_build_timeline_merge():
    tr = Transcript(items=[
        TranscriptItem(start=1.0, end=3.0, text="你到底是谁"),
        TranscriptItem(start=35.0, end=38.0, text="我是你找的人"),
        TranscriptItem(start=70.0, end=72.0, text="再见"),
    ])
    visuals = [
        {"t": 2.0, "desc": "男人在门口质问"},
        {"t": 40.0, "desc": "两人在雨中对峙"},
        {"t": 100.0, "desc": "空镜街道"},
    ]
    cuts = [SceneCut(time_sec=30.0, score=0.5), SceneCut(time_sec=65.0, score=0.6)]

    tl = build_timeline("m1", 120.0, transcript=tr, frame_descs=visuals, cuts=cuts, bucket=30.0)

    # 边界 0/30/65/120（65 来自场景切点，30 与 bucket 重合）
    assert tl.cut_times == [30.0, 65.0]
    all_dialog = " ".join(e.dialog for e in tl.entries)
    assert "你到底是谁" in all_dialog
    assert "我是你找的人" in all_dialog
    # 台词与画面同窗聚合
    e0 = next(e for e in tl.entries if e.start == 0.0)
    assert "男人在门口质问" in e0.visual
    assert "你到底是谁" in e0.dialog
    # t=100 的画面在最后窗口
    assert any("空镜街道" in e.visual for e in tl.entries)
    # 无台词无画面的窗口被跳过
    assert all(e.summary for e in tl.entries)


def test_timeline_roundtrip(tmp_path):
    tl = build_timeline("m", 60.0, transcript=Transcript(items=[
        TranscriptItem(start=0, end=2, text="你好")
    ]), cuts=[20.0])
    p = tl.save(tmp_path / "tl.json")
    tl2 = Timeline_load(p)
    assert tl2.media_id == "m"
    assert len(tl2.entries) == len(tl.entries)


def Timeline_load(p):
    from taggycat.timeline import Timeline

    return Timeline.load(p)


def test_timeline_empty():
    tl = build_timeline("m", 10.0)
    assert tl.entries == []


# ---------------------------------------------------------------------
# 阶段：asr（字幕轨路径）+ understand（无 VLM 降级）
# ---------------------------------------------------------------------
@pytest.fixture
def config(tmp_path) -> "object":
    cfg = load_config(ROOT / "config.example.toml")
    cfg.paths.workspace = str(tmp_path / "ws")
    cfg.vlm.enabled = True
    cfg.vlm.api_key = ""            # 强制降级（无 VLM）
    cfg.asr.enabled = False         # 无 whisper 模型时靠字幕轨
    cfg.vlm.max_frames = 4
    cfg.vlm.frame_interval = 2.0
    cfg.material.scene_threshold = 0.0
    return cfg


@pytest.fixture
def video_with_srt(tmp_path):
    if not HAS_FFMPEG:
        pytest.skip("no ffmpeg")
    mp4 = tmp_path / "movie.mp4"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-y",
         "-f", "lavfi", "-i", "testsrc=duration=6:size=320x240:rate=10",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=6",
         "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
         "-c:a", "aac", str(mp4)],
        check=True, capture_output=True,
    )
    srt = mp4.with_suffix(".srt")
    srt.write_text(
        "1\n00:00:00,500 --> 00:00:02,000\n你到底是谁？\n\n"
        "2\n00:00:03,000 --> 00:00:05,000\n我是来找你的。\n",
        encoding="utf-8",
    )
    return str(mp4)


def test_asr_stage_uses_subtitle(config, video_with_srt):
    store = TaskStore(config)
    st = store.create([video_with_srt], name="asr_sub")
    p = build_pipeline(config)
    p.run(st.task_id, only_stage="probe", force=True)
    p.run(st.task_id, only_stage="subtitle", force=True)
    final = p.run(st.task_id, only_stage="asr", force=True)

    assert final.stages["asr"].status == StageStatus.DONE
    tr_path = p.store.stage_dir(st.task_id, "asr") / f"{st.sources[0].media_id}.json"
    assert tr_path.exists()
    tr = Transcript.load(tr_path)
    assert len(tr.items) == 2
    assert "你到底是谁" in tr.items[0].text
    assert final.stages["asr"].data["items"][0]["source"] == "subtitle"


def test_understand_stage_degrades_without_vlm(config, video_with_srt):
    store = TaskStore(config)
    st = store.create([video_with_srt], name="und")
    p = build_pipeline(config)
    for s in ("probe", "subtitle", "asr", "frames"):
        p.run(st.task_id, only_stage=s, force=True)
    final = p.run(st.task_id, only_stage="understand", force=True)

    assert final.stages["understand"].status == StageStatus.DONE
    tl_file = p.store.stage_dir(st.task_id, "understand") / f"{st.sources[0].media_id}.json"
    assert tl_file.exists()
    data = json.loads(tl_file.read_text(encoding="utf-8"))
    assert data["entries"]
    assert any(e["dialog"] for e in data["entries"])
    # 无 VLM → 无 visual
    assert not any(e["visual"] for e in data["entries"])
    assert final.stages["understand"].data["any_vlm"] is False


def test_full_m2_run_stops_at_analyze(config, video_with_srt):
    """M2 全链路：probe→…→understand 完成（不触发 M3 的 LLM 调用）。"""
    store = TaskStore(config)
    st = store.create([video_with_srt], name="m2")
    p = build_pipeline(config)
    final = p.run(st.task_id, until="understand")

    for s in ("probe", "subtitle", "asr", "frames", "understand"):
        assert final.stages[s].status == StageStatus.DONE, f"{s} 未完成"
    assert final.current_stage == "understand"


def test_understand_reports_frame_progress(config, video_with_srt, monkeypatch):
    """understand 阶段内按帧上报细分进度（任务表 understand x/y帧）。"""
    store = TaskStore(config)
    st = store.create([video_with_srt], name="prog")
    p = build_pipeline(config)
    for s in ("probe", "subtitle", "asr", "frames"):
        p.run(st.task_id, only_stage=s, force=True)

    snapshots: list[dict] = []

    class FakeVLM:
        def describe_batch(self, frames, *, batch_size=6, style_hint="",
                           on_progress=None):
            assert callable(on_progress), "run_understand 应传入 on_progress"
            half = max(1, len(frames) // 2)
            on_progress(half, len(frames))
            snapshots.append(dict(
                p.store.load(st.task_id).stage("understand").data.get("progress") or {}
            ))
            on_progress(len(frames), len(frames))
            return [{"t": t, "desc": "昏暗的房间"} for t, _ in frames]

    monkeypatch.setattr("taggycat.llm.get_vlm", lambda cfg: FakeVLM())
    final = p.run(st.task_id, only_stage="understand", force=True)

    assert final.stages["understand"].status == StageStatus.DONE
    assert snapshots, "回调期间应已把 progress 落盘"
    prog = snapshots[0]
    assert prog.get("label") == "帧"
    assert prog.get("total", 0) > 0
    assert 0 < prog["done"] <= prog["total"]
    # 阶段完成后细分进度被最终汇总覆盖
    assert "progress" not in final.stages["understand"].data
    # 画面时间线覆盖率已统计（讲解-画面匹配的数据质量指标）
    cov = final.stages["understand"].data.get("visual_coverage")
    assert cov is not None and cov > 0
