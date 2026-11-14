"""M3 测试：剧情分析/分段规划/文案/匹配/审核（Mock LLM）。"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from taggycat.config import load_config
from taggycat.models import Segment, StageStatus
from taggycat.pipeline import ReviewRequired, build_pipeline
from taggycat.store import TaskStore

HAS_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
ROOT = Path(__file__).resolve().parent.parent
pytestmark = pytest.mark.skipif(not HAS_FFMPEG, reason="需要 ffmpeg/ffprobe")


class FakeLLM:
    """按提示词内容返回固定 JSON 的假 LLM。"""

    def __init__(self):
        self.calls: list[str] = []
        self.model = "fake"

    def chat_json(self, messages, **kw):
        user = next(m["content"] for m in messages if m["role"] == "user")
        self.calls.append(user[:50])
        if "剧情时间线" in user:          # analyze
            return {
                "synopsis": "小人物意外卷入阴谋，绝地反击。",
                "characters": [{"name": "男主", "desc": "外卖员"}],
                "highlights": [{"start": 1.0, "end": 4.0, "reason": "冲突"}],
                "tone_advice": ["少即是多"],
            }
        if "候选片段" in user:            # plan
            import re

            ids = re.findall(r"(c\d{3})", user)
            picked = ids[:4]
            return {"chosen": [{"id": i, "reason": "关键剧情"} for i in picked]}
        if "待写文案片段" in user:        # script
            import re

            ids = re.findall(r"id=(s\d{3})", user)
            return [{"id": i, "caption": "他猛然抬头，" * 8} for i in ids]
        if "修改后的文案" in user or "目标字数" in user and "原文案" in user:  # repair
            return {"caption": "他猛然抬头，" * 10}
        return {}


@pytest.fixture
def config(tmp_path):
    cfg = load_config(ROOT / "config.example.toml")
    cfg.paths.workspace = str(tmp_path / "ws")
    cfg.paths.output = str(tmp_path / "out")
    cfg.paths.tts_cache = str(tmp_path / "tts_cache")
    cfg.vlm.api_key = ""
    cfg.asr.enabled = False
    cfg.vlm.max_frames = 4
    cfg.vlm.frame_interval = 2.0
    cfg.material.scene_threshold = 0.0
    # 缩小片段窗口，让 12s 测试片能切出多个候选
    cfg.material.clip_min_duration = 2.0
    cfg.material.clip_max_duration = 4.0
    return cfg


@pytest.fixture
def fake_llm(monkeypatch):
    fake = FakeLLM()
    monkeypatch.setattr("taggycat.llm.get_llm", lambda cfg: fake)
    # stages_m3 内部 `from taggycat.llm import get_llm` 按名导入，需替换模块属性
    monkeypatch.setattr(
        "taggycat.pipeline.stages_m3.get_llm", lambda cfg: fake, raising=False
    )
    return fake


@pytest.fixture
def video_with_srt(tmp_path):
    mp4 = tmp_path / "drama.mp4"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-y",
         "-f", "lavfi", "-i", "testsrc=duration=12:size=320x240:rate=10",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=12",
         "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
         "-c:a", "aac", str(mp4)],
        check=True, capture_output=True,
    )
    mp4.with_suffix(".srt").write_text(
        "1\n00:00:00,500 --> 00:00:03,000\n你到底是谁\n\n"
        "2\n00:00:04,000 --> 00:00:07,000\n我是来报仇的\n\n"
        "3\n00:00:08,000 --> 00:00:11,000\n这一切才刚开始\n",
        encoding="utf-8",
    )
    return str(mp4)


def _prepare(store, config, video, name="m3", validate=True, target=60):
    from taggycat.models import ProjectConfig

    st = store.create(
        [video], name=name,
        project=ProjectConfig(validate_script=validate, target_duration=target,
                              output_dir=str(Path(config.paths.workspace).parent / "out")),
    )
    p = build_pipeline(config)
    for s in ("probe", "subtitle", "asr", "frames", "understand"):
        p.run(st.task_id, only_stage=s, force=True)
    return p, st


def test_analyze_and_plan(config, video_with_srt, fake_llm):
    store = TaskStore(config)
    p, st = _prepare(store, config, video_with_srt, name="ap")

    final = p.run(st.task_id, only_stage="analyze", force=True)
    assert final.stages["analyze"].status == StageStatus.DONE
    analysis = json.loads(
        (p.store.stage_dir(st.task_id, "analyze") / "analysis.json").read_text(encoding="utf-8")
    )
    assert analysis["synopsis"]
    assert analysis["characters"]

    final = p.run(st.task_id, only_stage="plan", force=True)
    assert final.stages["plan"].status == StageStatus.DONE
    segs = json.loads(
        (p.store.stage_dir(st.task_id, "plan") / "segments.json").read_text(encoding="utf-8")
    )
    assert 3 <= len(segs) <= 6
    # 片段时间合法
    for s in segs:
        assert s["end"] > s["start"]
        assert s["source_id"]


def test_plan_fills_up_to_target_duration(config, video_with_srt, monkeypatch):
    """LLM 只选 1 个片段时，plan 自动补选直到 ≥ 目标 90%（修复：成片远短于目标）。"""
    store = TaskStore(config)
    p, st = _prepare(store, config, video_with_srt, name="fill", target=12)

    class OnePickLLM:
        model = "fake"

        def chat_json(self, messages, **kw):
            import re

            user = next(m["content"] for m in messages if m["role"] == "user")
            ids = re.findall(r"(c\d{3})", user)
            return {"chosen": [{"id": ids[0], "reason": "开场钩子"}]}

    fake = OnePickLLM()
    monkeypatch.setattr("taggycat.llm.get_llm", lambda cfg: fake)
    monkeypatch.setattr(
        "taggycat.pipeline.stages_m3.get_llm", lambda cfg: fake, raising=False
    )

    final = p.run(st.task_id, only_stage="plan", force=True)
    assert final.stages["plan"].status == StageStatus.DONE
    segs = json.loads(
        (p.store.stage_dir(st.task_id, "plan") / "segments.json")
        .read_text(encoding="utf-8")
    )
    total = sum(s["end"] - s["start"] for s in segs)
    assert total >= 12 * 0.9 - 1e-6          # 补到 ≥ 目标 90%
    assert len(segs) >= 3
    assert any(s["reason"] == "时长补足" for s in segs)   # 触发了兜底补选
    # plan 预算 = 目标时长（不再被素材选取比例削减）
    assert final.stages["plan"].data["budget"] == 12


def test_sample_evenly_covers_full_source():
    """素材过剩时候选均匀采样：时长≈上限、覆盖到片尾、保持时间顺序。"""
    from taggycat.pipeline.stages_m3 import _sample_evenly

    cands = [
        {"id": f"c{i:03d}", "media_id": "m1", "source": "a.mp4",
         "start": float(i * 10), "end": float(i * 10 + 10), "summary": ""}
        for i in range(100)                    # 共 1000s
    ]
    kept = _sample_evenly(cands, 300.0)
    total = sum(c["end"] - c["start"] for c in kept)
    assert 250 <= total <= 350                 # ≈ 300s 上限
    assert kept[0]["start"] == 0.0             # 开头保留
    assert kept[-1]["end"] == 1000.0           # 片尾覆盖
    ids = [c["id"] for c in kept]
    assert ids == sorted(ids)                  # 时间顺序不乱
    # 不过剩时不削减
    assert len(_sample_evenly(cands[:10], 300.0)) == 10


def test_build_candidates_aggregates_window_visuals(tmp_path):
    """候选 summary 聚合时间窗内全部画面描述（选材依据不再只取首条）。"""
    from types import SimpleNamespace

    from taggycat.config import MaterialConfig
    from taggycat.models import MediaInfo, StageName
    from taggycat.pipeline.stages_m3 import _build_candidates
    from taggycat.timeline import Timeline, TimelineEntry

    media_id = "m1"
    (tmp_path / StageName.PROBE.value).mkdir(parents=True, exist_ok=True)
    (tmp_path / StageName.UNDERSTAND.value).mkdir(parents=True, exist_ok=True)
    (tmp_path / StageName.PROBE.value / f"{media_id}.json").write_text(
        MediaInfo(path="a.mp4", duration=20.0).model_dump_json(), encoding="utf-8"
    )
    tl = Timeline(media_id=media_id, duration=20.0, entries=[
        TimelineEntry(start=0.0, end=1.5, visual="男子翻文件"),
        TimelineEntry(start=1.5, end=3.0, visual="女子递过信封"),
        TimelineEntry(start=3.0, end=6.0, visual="走廊追逐"),
    ])
    tl.save(tmp_path / StageName.UNDERSTAND.value / f"{media_id}.json")

    ctx = SimpleNamespace(
        state=SimpleNamespace(
            sources=[SimpleNamespace(media_id=media_id, name="a.mp4")]
        ),
        stage_dir=lambda stage: tmp_path / stage.value,
        config=SimpleNamespace(material=MaterialConfig(
            clip_min_duration=2.0, clip_max_duration=4.0,
        )),
    )
    cands = _build_candidates(ctx)
    assert cands
    assert "男子翻文件" in cands[0]["summary"]
    assert "女子递过信封" in cands[0]["summary"]      # 同窗多条画面均聚合
    assert any("走廊追逐" in c["summary"] for c in cands[1:])


class _SeqLLM:
    """按调用次序返回固定结果的假 LLM（用于数组返回/纠正重试场景）。"""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = 0
        self.model = "fake"

    def chat_json(self, messages, **kw):
        r = self.replies[min(self.calls, len(self.replies) - 1)]
        self.calls += 1
        return r


def test_analyze_normalizes_array_and_retries(config, video_with_srt, monkeypatch):
    """本地模型只回 characters 数组 → 归一 + 纠正重试拿回完整对象。"""
    store = TaskStore(config)
    p, st = _prepare(store, config, video_with_srt, name="an_list")

    llm = _SeqLLM([
        [{"name": "男主", "desc": "外卖员"}],          # 第 1 次：只回数组
        {"synopsis": "小人物卷入阴谋。",                # 第 2 次：纠正后完整
         "characters": [{"name": "男主", "desc": "外卖员"}],
         "highlights": [{"start": 1.0, "end": 4.0, "reason": "冲突"}],
         "tone_advice": []},
    ])
    monkeypatch.setattr("taggycat.llm.get_llm", lambda cfg: llm)

    final = p.run(st.task_id, only_stage="analyze", force=True)
    assert final.stages["analyze"].status == StageStatus.DONE
    assert llm.calls == 2                        # 触发了一次纠正重试
    analysis = json.loads(
        (p.store.stage_dir(st.task_id, "analyze") / "analysis.json")
        .read_text(encoding="utf-8")
    )
    assert analysis["synopsis"] == "小人物卷入阴谋。"
    assert analysis["characters"][0]["name"] == "男主"
    assert analysis["highlights"][0]["end"] == 4.0


def test_analyze_array_twice_still_succeeds(config, video_with_srt, monkeypatch):
    """两次都只回数组也不能把任务打死：归一为 characters 后继续。"""
    store = TaskStore(config)
    p, st = _prepare(store, config, video_with_srt, name="an_arr2")

    llm = _SeqLLM([[{"name": "男主", "desc": "外卖员"}]])
    monkeypatch.setattr("taggycat.llm.get_llm", lambda cfg: llm)

    final = p.run(st.task_id, only_stage="analyze", force=True)
    assert final.stages["analyze"].status == StageStatus.DONE
    analysis = json.loads(
        (p.store.stage_dir(st.task_id, "analyze") / "analysis.json")
        .read_text(encoding="utf-8")
    )
    assert len(analysis["characters"]) == 1


def test_script_match_length(config, video_with_srt, fake_llm):
    store = TaskStore(config)
    p, st = _prepare(store, config, video_with_srt, name="sm")
    for s in ("analyze", "plan"):
        p.run(st.task_id, only_stage=s, force=True)

    final = p.run(st.task_id, only_stage="script", force=True)
    assert final.stages["script"].status == StageStatus.DONE
    segs = [Segment.model_validate(x) for x in json.loads(
        (p.store.stage_dir(st.task_id, "script") / "segments.json").read_text(encoding="utf-8")
    )]
    assert all(s.caption for s in segs)

    final = p.run(st.task_id, only_stage="match", force=True)
    assert final.stages["match"].status == StageStatus.DONE
    data = final.stages["match"].data
    assert "repaired" in data
    assert "still_bad" in data


def test_script_storyline_success(config, video_with_srt, fake_llm, monkeypatch):
    """故事节拍成功：storyline.json 落盘，script data storyline=True。"""
    store = TaskStore(config)
    p, st = _prepare(store, config, video_with_srt, name="sl-ok")
    for s in ("analyze", "plan"):
        p.run(st.task_id, only_stage=s, force=True)

    class StoryLLM(FakeLLM):
        def chat_json(self, messages, **kw):
            user = next(m["content"] for m in messages if m["role"] == "user")
            if "逐拍设计故事节拍" in user:
                ids = re.findall(r"id=(s\d{3})", user)
                return {"beats": [
                    {"id": i, "role": "主线推进", "point": "剧情推进一拍"}
                    for i in ids]}
            return super().chat_json(messages)

    monkeypatch.setattr("taggycat.llm.get_llm", lambda cfg: StoryLLM())
    final = p.run(st.task_id, only_stage="script", force=True)
    assert final.stages["script"].status == StageStatus.DONE
    assert final.stages["script"].data["storyline"] is True
    d = p.store.stage_dir(st.task_id, "script")
    assert (d / "storyline.json").exists()


def test_script_storyline_failure_nonblocking(config, video_with_srt, fake_llm):
    """节拍返回异常（FakeLLM 对该提示回 {}）→ 降级无骨架，不阻断文案。"""
    store = TaskStore(config)
    p, st = _prepare(store, config, video_with_srt, name="sl-bad")
    for s in ("analyze", "plan"):
        p.run(st.task_id, only_stage=s, force=True)
    final = p.run(st.task_id, only_stage="script", force=True)
    assert final.stages["script"].status == StageStatus.DONE
    assert final.stages["script"].data["storyline"] is False
    segs = json.loads(
        (p.store.stage_dir(st.task_id, "script") / "segments.json").read_text(encoding="utf-8")
    )
    assert all(s.get("caption") for s in segs)


class _ArcLLM(FakeLLM):
    """故事线返回 arc/protagonist + beats；可记录 script 阶段提示词。"""

    ARC = "外卖员要在午夜前洗清冤屈，否则被真凶灭口"

    def __init__(self):
        super().__init__()
        self.script_users: list[str] = []

    def chat_json(self, messages, **kw):
        user = next(m["content"] for m in messages if m["role"] == "user")
        if "逐拍设计故事节拍" in user:
            ids = re.findall(r"id=(s\d{3})", user)
            return {
                "arc": self.ARC,
                "protagonist": "男主",
                "beats": [
                    {"id": i, "role": "主线推进", "point": "剧情推进一拍"}
                    for i in ids],
            }
        if "待写文案片段" in user:
            self.script_users.append(user)
        return super().chat_json(messages)


def test_script_injects_characters_and_arc(config, video_with_srt, fake_llm, monkeypatch):
    """文案提示词必须注入角色表与故事主线（故事性核心）。"""
    store = TaskStore(config)
    p, st = _prepare(store, config, video_with_srt, name="arc")
    for s in ("analyze", "plan"):
        p.run(st.task_id, only_stage=s, force=True)

    llm = _ArcLLM()
    monkeypatch.setattr("taggycat.llm.get_llm", lambda cfg: llm)
    monkeypatch.setattr(
        "taggycat.pipeline.stages_m3.get_llm", lambda cfg: llm, raising=False)
    final = p.run(st.task_id, only_stage="script", force=True)
    assert final.stages["script"].status == StageStatus.DONE

    assert llm.script_users, "未捕获到文案提示词"
    for user in llm.script_users:
        assert "男主（外卖员）" in user          # 角色表注入
        assert _ArcLLM.ARC in user               # 故事主线注入


def test_script_polish_applies_within_bounds(config, video_with_srt, fake_llm, monkeypatch):
    """润色：合格修改被应用并计入 data['polished']。"""
    store = TaskStore(config)
    p, st = _prepare(store, config, video_with_srt, name="polish")
    for s in ("analyze", "plan"):
        p.run(st.task_id, only_stage=s, force=True)

    original = "他猛然抬头，" * 8          # 48 字，FakeLLM 的固定文案
    polished_text = "他缓缓抬起头，" * 7    # 49 字（±15% 内）

    class PolishLLM(_ArcLLM):
        def chat_json(self, messages, **kw):
            user = next(m["content"] for m in messages if m["role"] == "user")
            if "全集文案" in user:              # polish pass
                ids = re.findall(r"(s\d{3})（", user)
                return [{"id": ids[0], "caption": polished_text}]
            return super().chat_json(messages)

    llm = PolishLLM()
    monkeypatch.setattr("taggycat.llm.get_llm", lambda cfg: llm)
    monkeypatch.setattr(
        "taggycat.pipeline.stages_m3.get_llm", lambda cfg: llm, raising=False)
    final = p.run(st.task_id, only_stage="script", force=True)
    data = final.stages["script"].data
    assert data["polished"] == 1
    segs = json.loads(
        (p.store.stage_dir(st.task_id, "script") / "segments.json").read_text(
            encoding="utf-8"))
    assert segs[0]["caption"] == polished_text
    assert segs[1]["caption"] == original       # 未修改段保持原文


def test_script_polish_rejects_oversized(config, video_with_srt, fake_llm, monkeypatch):
    """润色：字数变化超 ±15% 的改写被拒绝，保护时长匹配。"""
    store = TaskStore(config)
    p, st = _prepare(store, config, video_with_srt, name="polish-rej")
    for s in ("analyze", "plan"):
        p.run(st.task_id, only_stage=s, force=True)

    original = "他猛然抬头，" * 8

    class GreedyLLM(_ArcLLM):
        def chat_json(self, messages, **kw):
            user = next(m["content"] for m in messages if m["role"] == "user")
            if "全集文案" in user:
                ids = re.findall(r"(s\d{3})（", user)
                return [{"id": ids[0], "caption": "扩写，" * 40}]   # 80 字，超界
            return super().chat_json(messages)

    llm = GreedyLLM()
    monkeypatch.setattr("taggycat.llm.get_llm", lambda cfg: llm)
    monkeypatch.setattr(
        "taggycat.pipeline.stages_m3.get_llm", lambda cfg: llm, raising=False)
    final = p.run(st.task_id, only_stage="script", force=True)
    assert final.stages["script"].data["polished"] == 0
    segs = json.loads(
        (p.store.stage_dir(st.task_id, "script") / "segments.json").read_text(
            encoding="utf-8"))
    assert segs[0]["caption"] == original       # 拒绝后保持原文


def test_review_requires_approval_when_enabled(config, video_with_srt, fake_llm):
    """校验文案开：review 阶段优雅暂停等人工审核。"""
    store = TaskStore(config)
    p, st = _prepare(store, config, video_with_srt, name="rev", validate=True)
    for s in ("analyze", "plan", "script", "match"):
        p.run(st.task_id, only_stage=s, force=True)

    final = p.run(st.task_id, only_stage="review", force=True)
    assert final.stages["review"].status == StageStatus.REVIEW
    review_dir = p.store.stage_dir(st.task_id, "review")
    assert (review_dir / "pending.json").exists()
    assert (review_dir / "PENDING.txt").exists()
    assert not (review_dir / "approved.json").exists()

    # 模拟 approve CLI 行为
    from taggycat.models import StageStatus as SS

    st2 = store.load(st.task_id)
    st2.stage("review").status = SS.DONE
    (review_dir / "approved.json").write_text('{"auto": false}', encoding="utf-8")
    store.save(st2)
    final = p.run(st.task_id, only_stage="review", force=True)
    assert final.stages["review"].status == SS.DONE


def test_review_disabled_passes_directly(config, video_with_srt, fake_llm):
    """校验文案关 + 字数合格：直接通过。"""
    store = TaskStore(config)
    p, st = _prepare(store, config, video_with_srt, name="auto", validate=False)
    for s in ("analyze", "plan", "script", "match"):
        p.run(st.task_id, only_stage=s, force=True)

    final = p.run(st.task_id, only_stage="review", force=True)
    assert final.stages["review"].status == StageStatus.DONE
    assert (p.store.stage_dir(st.task_id, "review") / "approved.json").exists()


def test_full_run_stops_at_review(config, video_with_srt, fake_llm):
    """M3 全链路：到 review 通过（关闭校验）为止（M4 阶段由 test_m4 覆盖）。"""
    store = TaskStore(config)
    p, st = _prepare(store, config, video_with_srt, name="full", validate=False)
    final = p.run(st.task_id, until="review")

    for s in ("probe", "subtitle", "asr", "frames", "understand",
              "analyze", "plan", "script", "match", "review"):
        assert final.stages[s].status == StageStatus.DONE, f"{s}: {final.stages[s].status}"
    assert final.current_stage == "review"
    assert not final.finished
    assert len(fake_llm.calls) >= 3


def test_review_required_exception_type():
    assert issubclass(ReviewRequired, RuntimeError)


def test_parse_script_items():
    from taggycat.pipeline.stages_m3 import _parse_script_items

    exp = ["s001", "s002"]
    # 正常返回
    assert _parse_script_items(
        [{"id": "s001", "caption": " A "}, {"id": "s002", "caption": "B"}], exp
    ) == {"s001": "A", "s002": "B"}
    # dict 包装取首个 list
    assert _parse_script_items({"items": [{"id": "s001", "caption": "A"}]},
                               ["s001"]) == {"s001": "A"}
    # 数字 id 归一化
    assert _parse_script_items([{"id": 1, "caption": "A"}], ["s001"]) == {"s001": "A"}
    assert _parse_script_items([{"id": "2", "caption": "B"}], ["s002"]) == {"s002": "B"}
    # id 全对不上但条数一致 → 按序回填（修复缺失→占位文案的根因）
    assert _parse_script_items(
        [{"id": "x1", "caption": "A"}, {"id": "x2", "caption": "B"}], exp
    ) == {"s001": "A", "s002": "B"}
    # 条数不一致不回填
    assert _parse_script_items([{"id": "x1", "caption": "A"}], exp) == {}
    # 非法输入
    assert _parse_script_items("nope", exp) == {}
    assert _parse_script_items([{"id": "s001"}], ["s001"]) == {}
    assert _parse_script_items([], exp) == {}


def test_parse_edge_rate():
    from taggycat.pipeline.stages_m3 import SPEECH_RATE, _parse_edge_rate

    assert _parse_edge_rate("") == SPEECH_RATE
    assert _parse_edge_rate("+0%") == SPEECH_RATE
    assert _parse_edge_rate("nonsense") == SPEECH_RATE
    assert _parse_edge_rate("+20%") == round(SPEECH_RATE * 1.2, 3)
    assert _parse_edge_rate("-10%") == round(SPEECH_RATE * 0.9, 3)


def test_script_rate_follows_style_for_edge_engine():
    from types import SimpleNamespace

    from taggycat.pipeline.stages_m3 import SPEECH_RATE, _script_rate
    from taggycat.prompts import get_style

    style = get_style("短剧漫剧")  # 快节奏风格，语速高于基准
    cfg = SimpleNamespace(tts=SimpleNamespace(
        engine="edge", edge=SimpleNamespace(rate="+0%")))
    # edge 引擎：按风格语速（与配音语速同源）
    ctx = SimpleNamespace(config=cfg, state=SimpleNamespace(
        project=SimpleNamespace(tts_engine="", rate="")))
    assert _script_rate(ctx, style) == style.rate
    # ComfyUI 引擎语速不可控 → 沿用基准，避免文案与配音时长脱节
    ctx2 = SimpleNamespace(config=cfg, state=SimpleNamespace(
        project=SimpleNamespace(tts_engine="comfyui", rate="")))
    assert _script_rate(ctx2, style) == SPEECH_RATE
    # 任务级语速覆盖优先于风格语速
    ctx3 = SimpleNamespace(config=cfg, state=SimpleNamespace(
        project=SimpleNamespace(tts_engine="", rate="-10%")))
    assert _script_rate(ctx3, style) == round(SPEECH_RATE * 0.9, 3)


def test_hook_report_flags_report_style_opening():
    from taggycat.pipeline.stages_m3 import _hook_report

    assert _hook_report([]) == ""
    ok = [Segment(segment_id="s001", source_id="a", start=0, end=3,
                  caption="他不知道，这一秒将改写所有人的结局。")]
    assert _hook_report(ok) == ""
    bad = [Segment(segment_id="s001", source_id="a", start=0, end=3,
                   caption="今天给大家讲一部电影，讲的是……")]
    issue = _hook_report(bad)
    assert issue and "报幕" in issue
