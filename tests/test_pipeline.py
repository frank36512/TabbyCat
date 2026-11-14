"""流水线编排器与任务存储测试。"""

import shutil
from pathlib import Path

import pytest

from taggycat.config import AppConfig, load_config
from taggycat.models import STAGE_ORDER, ProjectConfig, StageStatus
from taggycat.pipeline import build_pipeline
from taggycat.store import TaskStore

HAS_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
pytestmark = pytest.mark.skipif(not HAS_FFMPEG, reason="需要 ffmpeg/ffprobe")


@pytest.fixture
def config(tmp_path) -> AppConfig:
    cfg = load_config(Path(__file__).resolve().parent.parent / "config.example.toml")
    cfg.paths.workspace = str(tmp_path / "ws")
    cfg.vlm.max_frames = 5
    cfg.vlm.frame_interval = 2.0
    cfg.asr.enabled = False          # 测试不下载/加载 whisper 模型
    cfg.material.scene_threshold = 0.0  # 关闭场景检测（测试视频为纯色变化）
    return cfg


@pytest.fixture
def sample_video(tmp_path) -> str:
    if not HAS_FFMPEG:
        pytest.skip("no ffmpeg")
    path = tmp_path / "in.mp4"
    subprocess = __import__("subprocess")
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-y",
            "-f", "lavfi", "-i", "testsrc=duration=4:size=320x240:rate=10",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=4",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", str(path),
        ],
        check=True, capture_output=True,
    )
    return str(path)


def test_task_create_and_reload(config, sample_video):
    store = TaskStore(config)
    st = store.create([sample_video], name="demo", project=ProjectConfig(style="电影解说"))
    assert store.exists(st.task_id)
    # 所有阶段初始化为 pending
    assert len(st.stages) == len(STAGE_ORDER)
    assert all(s.status == StageStatus.PENDING for s in st.stages.values())

    st2 = store.load(st.task_id)
    assert st2.name == "demo"
    assert st2.project.style == "电影解说"
    assert st2.sources[0].size_mb > 0

    # 列表
    tasks = store.list()
    assert any(t.task_id == st.task_id for t in tasks)

    # 前缀解析
    assert store.resolve_task_id(st.task_id[:10]) == st.task_id


def test_task_create_missing_source(config, tmp_path):
    store = TaskStore(config)
    with pytest.raises(FileNotFoundError):
        store.create([tmp_path / "ghost.mp4"])


def test_pipeline_runs_implemented_stages(config, sample_video):
    store = TaskStore(config)
    st = store.create([sample_video], name="run")
    p = build_pipeline(config)

    final = p.run(st.task_id, until="understand")

    # M2 已实现：probe→understand
    for s in ("probe", "subtitle", "asr", "frames", "understand"):
        assert final.stages[s].status == StageStatus.DONE, f"{s} 未完成"
    assert final.current_stage == "understand"
    assert not final.finished

    # probe 产物存在
    probe_dir = p.store.stage_dir(st.task_id, "probe")
    assert (probe_dir / "_summary.json").exists()


def test_pipeline_single_stage(config, sample_video):
    store = TaskStore(config)
    st = store.create([sample_video], name="single")
    p = build_pipeline(config)

    final = p.run(st.task_id, only_stage="probe", force=True)
    assert final.stages["probe"].status == StageStatus.DONE

    # 单跑 frames（跳过 asr 拦截）
    final = p.run(st.task_id, only_stage="frames", force=True)
    assert final.stages["frames"].status == StageStatus.DONE
    frames_dir = p.store.stage_dir(st.task_id, "frames")
    manifest = frames_dir / "_manifest.json"
    assert manifest.exists()

    # 断点：再跑一次 probe（非 force 时 only_stage 会强制重跑，验证 done 不变）
    final = p.run(st.task_id, only_stage="probe")
    assert final.stages["probe"].status == StageStatus.DONE


def test_pipeline_skip_done_on_rerun(config, sample_video):
    store = TaskStore(config)
    st = store.create([sample_video], name="resume")
    p = build_pipeline(config)
    p.run(st.task_id, until="understand")

    # 第二次 run：probe 已 done，应跳过（finished_at 不变）
    s1 = store.load(st.task_id)
    t1 = s1.stages["probe"].finished_at
    p.run(st.task_id, until="understand")
    s2 = store.load(st.task_id)
    assert s2.stages["probe"].finished_at == t1

    # force 会重跑
    p.run(st.task_id, only_stage="probe", force=True)
    s3 = store.load(st.task_id)
    assert s3.stages["probe"].status == StageStatus.DONE


def test_unknown_stage(config, sample_video):
    store = TaskStore(config)
    st = store.create([sample_video], name="bad")
    p = build_pipeline(config)
    with pytest.raises(KeyError):
        p.run(st.task_id, only_stage="nope")


def _make_state(**kw):
    from taggycat.models import TaskState

    return TaskState(
        task_id="t1", name="n", created_at="", updated_at="",
        project=ProjectConfig(), **kw,
    )


def test_derive_task_state_branches():
    from taggycat.models import derive_task_state

    assert derive_task_state(_make_state()) == "等待中"

    t = _make_state()
    t.stage(STAGE_ORDER[0]).status = StageStatus.RUNNING
    assert derive_task_state(t) == "运行中"

    t = _make_state()
    t.stage("review").status = StageStatus.REVIEW
    assert derive_task_state(t) == "待审核"

    t = _make_state()
    t.stage(STAGE_ORDER[0]).status = StageStatus.FAILED
    assert derive_task_state(t) == "失败"

    assert derive_task_state(_make_state(finished=True)) == "已完成"

    # 归档优先于一切
    t = _make_state(archived=True, finished=True)
    t.stage(STAGE_ORDER[0]).status = StageStatus.FAILED
    assert derive_task_state(t) == "已归档"

    # 失败优先于待审核
    t = _make_state()
    t.stage(STAGE_ORDER[0]).status = StageStatus.FAILED
    t.stage("review").status = StageStatus.REVIEW
    assert derive_task_state(t) == "失败"


def test_full_probe_once_per_source(config, sample_video, monkeypatch):
    """TR-13.1: probe→frames 链路每源全参 ffprobe 恰好 1 次（后续阶段复用落盘信息）。"""
    import taggycat.media.ffmpeg as ffmod
    import taggycat.pipeline.stages as stages_mod

    real_probe = ffmod.probe
    calls: list[str] = []

    def counting(path):
        calls.append(str(path))
        return real_probe(path)

    monkeypatch.setattr(ffmod, "probe", counting)       # ffmpeg 内部调用
    monkeypatch.setattr(stages_mod, "probe", counting)  # stages.py 直接导入名

    store = TaskStore(config)
    st = store.create([sample_video], name="probe_once")
    p = build_pipeline(config)
    for s in ("probe", "subtitle", "asr", "frames"):
        final = p.run(st.task_id, only_stage=s, force=True)
        assert final.stages[s].status == StageStatus.DONE, f"{s} 未完成"

    assert calls.count(sample_video) == 1, f"全参 ffprobe 次数: {calls}"


def test_frames_stage_processes_at_most_two(config, sample_video, monkeypatch):
    """TR-14.1: frames 阶段 ffmpeg 进程数 ≤2（场景切分+场景帧合并为一次遍历）。"""
    import taggycat.media.ffmpeg as ffmod

    real_run = ffmod.run_command
    n = {"i": 0}

    def counting(cmd, **kw):
        n["i"] += 1
        return real_run(cmd, **kw)

    monkeypatch.setattr(ffmod, "run_command", counting)

    store = TaskStore(config)
    st = store.create([sample_video], name="proc2")
    p = build_pipeline(config)
    for s in ("probe", "subtitle", "asr"):
        p.run(st.task_id, only_stage=s, force=True)

    n["i"] = 0
    final = p.run(st.task_id, only_stage="frames", force=True)
    assert final.stages["frames"].status == StageStatus.DONE
    assert n["i"] <= 2, f"frames 阶段进程数: {n['i']}"
