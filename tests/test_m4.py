"""M4 测试：TTS（Mock 引擎）/ 字幕时间轴 / 合成导出全链路。"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from taggycat.config import load_config
from taggycat.models import Segment, StageStatus
from taggycat.pipeline import build_pipeline
from taggycat.store import TaskStore

HAS_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
ROOT = Path(__file__).resolve().parent.parent
pytestmark = pytest.mark.skipif(not HAS_FFMPEG, reason="需要 ffmpeg/ffprobe")


# ---------------------------------------------------------------------
# Mock：LLM 与 TTS
# ---------------------------------------------------------------------
class FakeLLM:
    def __init__(self):
        self.calls: list[str] = []
        self.model = "fake"

    def chat_json(self, messages, **kw):
        import re

        user = next(m["content"] for m in messages if m["role"] == "user")
        self.calls.append(user[:40])
        if "剧情时间线" in user:
            return {"synopsis": "小人物卷入阴谋绝地反击。", "characters": [],
                    "highlights": [], "tone_advice": []}
        if "候选片段" in user:
            ids = re.findall(r"(c\d{3})", user)[:4]
            return {"chosen": [{"id": i, "reason": "关键"} for i in ids]}
        if "待写文案片段" in user:
            ids = re.findall(r"id=(s\d{3}) 时长=([\d.]+)s", user)
            out = []
            for sid, dur in ids:
                # 按 4.5 字/秒给足字数，避免触发 repair 分支
                n = max(6, int(float(dur) * 4.5))
                out.append({"id": sid, "caption": ("他猛然抬头看向远方。" * 4)[:n]})
            return out
        return {}


class FakeTTS:
    """按字数生成正弦波 wav 的假引擎（4.5 字/秒）。"""

    name = "fake"
    ext = ".wav"

    def __init__(self):
        self.synthesized: list[str] = []
        self.voices: list[str | None] = []

    def synthesize(self, text: str, out_base: Path, voice: str | None = None) -> Path:
        from taggycat.media.ffmpeg import ffmpeg_bin

        self.synthesized.append(text)
        self.voices.append(voice)
        dur = max(0.5, len(text) / 4.5)
        out = out_base.with_suffix(self.ext)
        out.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            [ffmpeg_bin(), "-hide_banner", "-y",
             "-f", "lavfi", "-i", f"sine=frequency=440:duration={dur:.3f}",
             "-ar", "44100", "-ac", "2", str(out)],
            check=True, capture_output=True,
        )
        return out


@pytest.fixture
def config(tmp_path):
    cfg = load_config(ROOT / "config.example.toml")
    cfg.paths.workspace = str(tmp_path / "ws")
    cfg.paths.tts_cache = str(tmp_path / "tts_cache")
    cfg.vlm.api_key = ""
    cfg.vlm.enabled = False
    cfg.asr.enabled = False
    cfg.vlm.max_frames = 4
    cfg.vlm.frame_interval = 2.0
    cfg.material.scene_threshold = 0.0
    cfg.material.clip_min_duration = 2.0
    cfg.material.clip_max_duration = 4.0
    # 成片参数调小加快测试
    cfg.output.width = 320
    cfg.output.height = 240
    cfg.output.fps = 10
    cfg.output.crf = 30
    cfg.output.preset = "ultrafast"
    cfg.output.subtitle = True
    return cfg


@pytest.fixture
def fake_llm(monkeypatch):
    fake = FakeLLM()
    monkeypatch.setattr("taggycat.llm.get_llm", lambda cfg: fake)
    return fake


@pytest.fixture
def fake_tts(monkeypatch):
    fake = FakeTTS()
    monkeypatch.setattr("taggycat.tts.get_tts", lambda cfg: fake)
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
        "2\n00:00:04,000 --> 00:00:07,000\n我是来报仇的\n",
        encoding="utf-8",
    )
    return str(mp4)


def _prepare(store, config, video, name, validate=False):
    from taggycat.models import ProjectConfig

    st = store.create(
        [video], name=name,
        project=ProjectConfig(validate_script=validate, target_duration=60,
                              output_dir=str(Path(config.paths.workspace).parent / "out")),
    )
    p = build_pipeline(config)
    for s in ("probe", "subtitle", "asr", "frames", "understand",
              "analyze", "plan", "script", "match", "review"):
        final = p.run(st.task_id, only_stage=s, force=True)
        assert final.stages[s].status == StageStatus.DONE, \
            f"{s}: {final.stages[s].status} {final.stages[s].error}"
    return p, st


# ---------------------------------------------------------------------
# 单元
# ---------------------------------------------------------------------
def test_split_caption_strips_punctuation():
    from taggycat.pipeline.stages_m4 import split_caption

    chunks = split_caption("他猛然抬头，看向远方！三个人都惊呆了，谁也没说话。" * 3, 18)
    assert chunks
    assert all(len(c) <= 18 for c in chunks)
    punct = set("，。！？；、,.!?;：:“”\"'‘’…—–~·")
    for c in chunks:
        assert not set(c) & punct, f"字幕条含标点: {c!r}"


def test_load_final_segments_prefers_tts(tmp_path):
    """assemble/subtitle_gen 必须读 tts/segments.json（含 audio_path 与延长时长）。"""
    from types import SimpleNamespace

    from taggycat.models import StageName
    from taggycat.pipeline.stages_m4 import _load_final_segments

    def mk(stage: StageName, payload: dict) -> None:
        d = tmp_path / stage.value
        d.mkdir(parents=True, exist_ok=True)
        (d / "segments.json").write_text(json.dumps([payload]), encoding="utf-8")

    base = {"segment_id": "s001", "source_id": "m1", "start": 0.0, "clip_duration": 5.0}
    mk(StageName.MATCH, dict(base))
    mk(StageName.TTS, dict(base, audio_path="voice.mp3", voice_duration=4.0, duration=5.4))
    ctx = SimpleNamespace(stage_dir=lambda s: tmp_path / s.value)

    segs = _load_final_segments(ctx, include_audio=True)
    assert segs[0].audio_path == "voice.mp3"
    assert segs[0].duration == 5.4
    # 默认（tts 阶段自身）取最新文案，不带旧配音信息
    segs2 = _load_final_segments(ctx)
    assert not segs2[0].audio_path


def test_segment_cmd_original_audio_modes(tmp_path):
    """原声三档：keep 保留 / mute 全消音 / tail 仅段尾保留一小段。"""
    from types import SimpleNamespace

    from taggycat.config import OutputConfig
    from taggycat.models import Segment
    from taggycat.pipeline.stages_m4 import _segment_cmd

    voice = tmp_path / "v.wav"
    voice.write_bytes(b"x")
    seg = Segment(segment_id="s001", source_id="m1", start=0.0, clip_duration=5.0,
                  duration=5.4, audio_path=str(voice), voice_duration=5.0)

    def cmd(mode: str, keep: bool = True) -> str:
        o = OutputConfig(original_audio_mode=mode, keep_original_audio=keep)
        ctx = SimpleNamespace(config=SimpleNamespace(output=o))
        return " ".join(_segment_cmd(ctx, seg, tmp_path / "src.mp4", True,
                                     tmp_path / "out.mp4"))

    keep_c = cmd("keep")
    assert "volume=0.3[a1]" in keep_c and "adelay" not in keep_c
    mute_c = cmd("mute")
    assert "[0:a]" not in mute_c              # 原声完全不进命令
    assert "[1:a]" in mute_c and "[a2]" in mute_c
    tail_c = cmd("tail")
    # 段尾窗口开关（adelay 会写坏音轨 stts，禁用）
    assert "volume=0.3:enable='between(t,4.200,5.400)'" in tail_c
    assert "adelay" not in tail_c
    assert "volume=0.3[a1]" in cmd("")        # 空 mode → 跟随 keep_original_audio
    assert "[0:a]" not in cmd("", keep=False)  # → mute


def test_segment_cmd_hard_sub_delogo(tmp_path):
    """讲解段遮盖原片烧录字幕；原声让路段/开关关闭时不加遮盖。

    默认遮盖方式为 blur（boxblur）；hard_sub_mode="delogo" 时走 delogo 插值。
    """
    from types import SimpleNamespace

    from taggycat.config import OutputConfig
    from taggycat.models import Segment
    from taggycat.pipeline.stages_m4 import _segment_cmd

    voiced = Segment(segment_id="s001", source_id="m1", start=0.0, end=5.0,
                     duration=5.4, audio_path="", voice_duration=5.0)
    yielding = Segment(segment_id="s002", source_id="m1", start=6.0, end=11.0,
                       duration=5.0, audio_path="", keep_audio=True)

    def cmd(seg, **o_kw) -> str:
        o = OutputConfig(**o_kw)
        ctx = SimpleNamespace(config=SimpleNamespace(output=o))
        return " ".join(_segment_cmd(ctx, seg, tmp_path / "src.mp4", True,
                                     tmp_path / "out.mp4"))

    c = cmd(voiced)                                   # 默认 hard_sub_mode="blur"
    assert "boxblur=" in c and "delogo=" not in c
    assert "delogo=" in cmd(voiced, hard_sub_mode="delogo")
    assert "delogo=" not in cmd(yielding)             # 让路段保留原字幕
    assert "boxblur=" not in cmd(yielding)
    assert "boxblur=" not in cmd(voiced, hard_sub_remove=False)

    # 遮盖矩形必须完整落在画面内（delogo 不允许贴边）
    o = OutputConfig(hard_sub_mode="delogo")
    x, y, w, h = o.hard_sub_rect()
    assert x >= 1 and y >= 1 and x + w <= o.width - 1 and y + h <= o.height - 1
    assert OutputConfig(hard_sub_band="bad").hard_sub_rect() is None
    assert OutputConfig(hard_sub_band="0.9,0.1,0.2,0.9").hard_sub_rect() is None

    # 默认遮盖带必须延伸到画面最底（原片字幕可低至贴边行；旧默认 y2=0.97 漏字）
    x0, y0, w0, h0 = OutputConfig().hard_sub_rect()
    assert y0 + h0 >= OutputConfig().height - 1


def test_extend_for_voice_limits(tmp_path):
    """配音超长片段向后延伸：受相邻入点/源片末尾钳制，让路段不延伸。"""
    from taggycat.models import Segment
    from taggycat.pipeline.stages_m4 import _extend_for_voice

    # a 需要 8.4-5=3.4s，但下一选中段入点 7.0 → 只能到 7.0（仍差 → partial）
    a = Segment(segment_id="s001", source_id="m1", start=0.0, end=5.0,
                audio_path="v1.wav", voice_duration=8.0, duration=8.4)
    b = Segment(segment_id="s002", source_id="m1", start=7.0, end=10.0,
                audio_path="v2.wav", voice_duration=2.0, duration=5.4)
    keep = Segment(segment_id="s003", source_id="m2", start=0.0, end=5.0,
                   audio_path="", keep_audio=True, duration=5.0)
    stats = _extend_for_voice([a, b, keep], {"m1": 60.0, "m2": 60.0}, {"m1": [], "m2": []})
    assert a.end == 7.0
    assert a.duration == 8.4                          # 仍需 tpad 兜底
    assert stats["segments"] == 1 and stats["partial"] == 1
    assert b.end == 10.0 and keep.end == 5.0          # 不超 need / 让路不动

    # 无相邻段：延到配音所需，且顺带停在 +1.5s 内的场景切点
    c = Segment(segment_id="s001", source_id="m3", start=0.0, end=5.0,
                audio_path="v.wav", voice_duration=9.0, duration=9.4)
    stats = _extend_for_voice([c], {"m3": 20.0}, {"m3": [10.0]})
    assert c.end == 10.0                              # need=4.4 → 9.4，切点10.0在+1.5内
    assert c.duration == 10.0                         # clip ≥ voice+gap，无定格
    assert stats == {"segments": 1, "seconds": 5.0, "partial": 0}


def test_comfyui_workflow_ui_format_rejected(tmp_path):
    from taggycat.config import TTSComfyConfig, TTSConfig
    from taggycat.tts import ComfyUITTS, TTSError

    wf = tmp_path / "wf.json"
    wf.write_text(json.dumps({"nodes": [], "links": []}), encoding="utf-8")
    ccfg = TTSComfyConfig(workflow=str(wf))
    eng = ComfyUITTS(TTSConfig(comfyui=ccfg))
    with pytest.raises(TTSError, match="API"):
        eng._load_workflow()


def test_comfyui_patch_prompt_text_input(tmp_path):
    from taggycat.config import TTSComfyConfig, TTSConfig
    from taggycat.tts import ComfyUITTS

    prompt = {
        "5": {"class_type": "CLIPTextEncode", "inputs": {"text": "占位", "clip": ["1", 0]}},
        "6": {"class_type": "KSampler", "inputs": {"seed": 42}},
    }
    eng = ComfyUITTS(TTSConfig(comfyui=TTSComfyConfig()))
    out = eng._patch_prompt(prompt, "新文案")
    assert out["5"]["inputs"]["text"] == "新文案"
    assert out["6"]["inputs"]["seed"] == 42  # 非文本输入不被改动


def test_comfyui_patch_prompt_missing_text_raises():
    from taggycat.config import TTSComfyConfig, TTSConfig
    from taggycat.tts import ComfyUITTS, TTSError

    prompt = {"1": {"class_type": "EmptyLatent", "inputs": {"batch_size": 1}}}
    eng = ComfyUITTS(TTSConfig(comfyui=TTSComfyConfig()))
    with pytest.raises(TTSError, match="文本输入"):
        eng._patch_prompt(prompt, "你好")


def test_clone_reference_audio_uploaded_for_remote(tmp_path, monkeypatch):
    """本地参考音频先上传到 ComfyUI input（远端工作流才读得到），且只传一次。"""
    from taggycat.config import TTSComfyConfig, TTSConfig
    from taggycat.tts import ComfyUITTS

    ref = tmp_path / "ref.mp3"
    ref.write_bytes(b"fake-audio")
    ccfg = TTSComfyConfig(reference_audio=str(ref), base_url="http://x:8188")
    eng = ComfyUITTS(TTSConfig(comfyui_clone=ccfg), clone=True, root_dir=tmp_path)

    calls: list = []
    monkeypatch.setattr(
        "taggycat.tts.upload",
        lambda base, p, **k: calls.append(p) or "ref.mp3",
    )
    assert eng._ref_for_comfy(str(ref)) == "ref.mp3"
    assert eng._ref_for_comfy(str(ref)) == "ref.mp3"
    assert len(calls) == 1                      # 同一实例重复合成不重复上传
    assert eng._ref_for_comfy("existing.mp3") == "existing.mp3"  # 端上已有文件名原样用


def test_tts_cache_key_stable():
    from taggycat.tts import cache_key

    a = cache_key("edge", "zh-CN-YunxiNeural", "+0%", "你好世界")
    b = cache_key("edge", "zh-CN-YunxiNeural", "+0%", "你好世界")
    c = cache_key("edge", "zh-CN-YunxiNeural", "+0%", "换个文案")
    assert a == b
    assert a != c


# ---------------------------------------------------------------------
# 阶段
# ---------------------------------------------------------------------
def test_tts_stage_and_cache(config, video_with_srt, fake_llm, fake_tts):
    store = TaskStore(config)
    p, st = _prepare(store, config, video_with_srt, name="tts")

    final = p.run(st.task_id, only_stage="tts", force=True)
    assert final.stages["tts"].status == StageStatus.DONE
    data = final.stages["tts"].data
    assert data["engine"] == "fake"
    assert data["voice_total"] > 0
    n_synth = len(fake_tts.synthesized)

    segs = [Segment.model_validate(x) for x in json.loads(
        (p.store.stage_dir(st.task_id, "tts") / "segments.json").read_text(encoding="utf-8")
    )]
    assert all(s.audio_path and Path(s.audio_path).exists() for s in segs)
    assert all(s.voice_duration > 0 for s in segs)
    assert all(s.duration >= s.clip_duration for s in segs)

    # 再跑一次：全部命中缓存，不再合成
    final = p.run(st.task_id, only_stage="tts", force=True)
    assert final.stages["tts"].data["cache_hits"] == len(segs)
    assert len(fake_tts.synthesized) == n_synth


def test_tts_bounded_concurrency(config, video_with_srt, fake_llm, monkeypatch):
    """TR-12.1: 慢引擎合成调用重叠（墙钟 < 串行 60%），瞬时并发不超上限。"""
    import threading
    import time as _time

    config.tts.max_workers = 3

    class SlowTTS(FakeTTS):
        name = "slow"

        def __init__(self):
            super().__init__()
            self._lock = threading.Lock()
            self.active = 0
            self.max_active = 0
            self.windows: list[tuple[float, float]] = []

        def synthesize(self, text, out_base, voice=None):
            with self._lock:
                self.active += 1
                self.max_active = max(self.max_active, self.active)
            t0 = _time.perf_counter()
            try:
                _time.sleep(0.3)
                return super().synthesize(text, out_base, voice=voice)
            finally:
                with self._lock:
                    self.active -= 1
                self.windows.append((t0, _time.perf_counter()))

    slow = SlowTTS()
    monkeypatch.setattr("taggycat.tts.get_tts", lambda cfg: slow)

    store = TaskStore(config)
    p, st = _prepare(store, config, video_with_srt, name="slowtts")
    final = p.run(st.task_id, only_stage="tts", force=True)
    assert final.stages["tts"].status == StageStatus.DONE

    n = len(slow.synthesized)
    assert n >= 3, f"计时测试需要 ≥3 个合成段，实际 {n}"
    assert slow.max_active <= 3

    busy = sum(e - s for s, e in slow.windows)
    span = max(e for _s, e in slow.windows) - min(s for s, _e in slow.windows)
    assert span < busy * 0.6, f"并发墙钟 {span:.2f}s ≥ 串行 {busy:.2f}s 的 60%"


def test_subtitle_gen_timeline(config, video_with_srt, fake_llm, fake_tts):
    from taggycat.media.sub import read_srt

    store = TaskStore(config)
    p, st = _prepare(store, config, video_with_srt, name="sub")
    for s in ("tts", "subtitle_gen"):
        final = p.run(st.task_id, only_stage=s, force=True)
        assert final.stages[s].status == StageStatus.DONE

    srt = p.store.stage_dir(st.task_id, "subtitle_gen") / "narration.srt"
    sub = read_srt(srt)
    assert sub.cues
    # 用户要求：口播字幕不带标点符号
    from taggycat.pipeline.stages_m4 import CAPTION_PUNCT

    for c in sub.cues:
        assert not set(c.text) & CAPTION_PUNCT, f"SRT 含标点: {c.text!r}"
    # 时间轴单调且不越界
    total = final.stages["subtitle_gen"].data["total_duration"]
    prev_end = -0.001
    for c in sub.cues:
        assert c.start >= prev_end - 0.05
        assert c.end <= total + 0.6
        prev_end = c.end
    assert any(len(c.text) <= 18 for c in sub.cues)


def test_full_m4_export(config, video_with_srt, fake_llm, fake_tts):
    """M4 全链路：TTS → 字幕 → 合成 → 导出成片（原声静音，成片必须有配音声）。"""
    import re as _re

    config.output.original_audio_mode = "mute"
    store = TaskStore(config)
    p, st = _prepare(store, config, video_with_srt, name="成片测试")
    final = p.run(st.task_id)

    for s in ("tts", "subtitle_gen", "assemble", "export"):
        assert final.stages[s].status == StageStatus.DONE, \
            f"{s}: {final.stages[s].status} {final.stages[s].error}"
    assert final.finished is True

    out = Path(final.stages["export"].data["output"])
    assert out.exists()
    assert out.stat().st_size > 10_000

    from taggycat.media.ffmpeg import media_duration

    dur = media_duration(out)
    expected = float(final.stages["assemble"].data["total_duration"])
    assert abs(dur - expected) < 1.5, f"成片 {dur}s vs 预期 {expected}s"
    assert final.stages["export"].data["subtitle_burned"] is True

    # 原声已 mute：成片仍有声 = 配音确实混入（修复 audio_path 丢失回归）
    # 注意：FakeTTS 正弦波本身 max ≈ -21dB，链路仅损 0.4dB；无配音时为 -91dB 静音
    r = subprocess.run(
        ["ffmpeg", "-hide_banner", "-i", str(out), "-af", "volumedetect",
         "-f", "null", "-"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    m = _re.search(r"max_volume: ([-\d.]+) dB", r.stderr)
    assert m and float(m.group(1)) > -40, f"成片疑似无配音声: {r.stderr[-200:]}"


def test_assemble_parallel_bounded(config, video_with_srt, fake_llm, fake_tts,
                                   monkeypatch):
    """TR-15.1: 段级并发执行、瞬时并发有界，base.mp4 时长与串行合计一致。"""
    import os
    import threading

    import taggycat.pipeline.stages_m4 as m4mod
    from taggycat.media.ffmpeg import media_duration

    store = TaskStore(config)
    p, st = _prepare(store, config, video_with_srt, name="asm_par")

    lock = threading.Lock()
    state = {"active": 0, "max": 0}
    real_run = m4mod.run_command

    def counting(cmd, **kw):
        with lock:
            state["active"] += 1
            state["max"] = max(state["max"], state["active"])
        try:
            return real_run(cmd, **kw)
        finally:
            with lock:
                state["active"] -= 1

    monkeypatch.setattr(m4mod, "run_command", counting)

    final = p.run(st.task_id, only_stage="assemble", force=True)
    assert final.stages["assemble"].status == StageStatus.DONE

    n = final.stages["assemble"].data["parts"]
    assert n >= 2, f"并行测试需要多段，实际 {n}"
    assert state["max"] >= 2, "片段未并发执行"
    cap = max(1, min(4, (os.cpu_count() or 2) // 2))
    assert state["max"] <= cap, f"瞬时并发 {state['max']} 超上限 {cap}"

    base = p.store.stage_dir(st.task_id, "assemble") / "base.mp4"
    assert base.exists()
    expected = float(final.stages["assemble"].data["total_duration"])
    assert abs(media_duration(base) - expected) < 1.0


def test_cached_audio_hit_and_miss(tmp_path):
    """TR-16.1: 按引擎扩展名 O(1) 判断，命中语义与旧 glob 实现一致。"""
    from taggycat.tts import cached_audio

    # 目录不存在 / 文件不存在 → None
    assert cached_audio(tmp_path, "deadbeef", ".wav") is None
    f = tmp_path / "deadbeef.wav"
    f.write_bytes(b"x")
    assert cached_audio(tmp_path, "deadbeef", ".wav") == f
    # 空文件不命中
    f.write_bytes(b"")
    assert cached_audio(tmp_path, "deadbeef", ".wav") is None
    # 扩展名不匹配不命中（旧实现 glob key.* 会匹配任意扩展名）
    f.write_bytes(b"x")
    assert cached_audio(tmp_path, "deadbeef", ".mp3") is None


def test_export_cleans_seg_files_keeps_base(config, video_with_srt, fake_llm,
                                            fake_tts):
    """TR-16.1: export 成功后 seg_*.mp4 已删，base.mp4 保留。"""
    store = TaskStore(config)
    p, st = _prepare(store, config, video_with_srt, name="cleanup")
    final = p.run(st.task_id)
    assert final.stages["export"].status == StageStatus.DONE

    asm = p.store.stage_dir(st.task_id, "assemble")
    assert (asm / "base.mp4").exists()
    assert list(asm.glob("seg_*.mp4")) == []


def test_export_without_subtitle(config, video_with_srt, fake_llm, fake_tts):
    config.output.subtitle = False
    store = TaskStore(config)
    p, st = _prepare(store, config, video_with_srt, name="nosub")
    final = p.run(st.task_id)
    assert final.stages["export"].status == StageStatus.DONE
    assert final.stages["export"].data["subtitle_burned"] is False
    assert Path(final.stages["export"].data["output"]).exists()


def test_tts_voice_and_engine_wiring(config, video_with_srt, fake_llm, fake_tts,
                                     monkeypatch):
    """任务级 voice / tts_engine 必须真正传到合成引擎（而非只进缓存键）。"""
    seen_engines: list[str] = []

    def fake_get_tts(cfg):
        seen_engines.append(cfg.tts.engine)
        return fake_tts

    monkeypatch.setattr("taggycat.tts.get_tts", fake_get_tts)

    store = TaskStore(config)
    p, st = _prepare(store, config, video_with_srt, name="voice")

    state = store.load(st.task_id)
    state.project.voice = "zh-CN-TestNeural"
    state.project.tts_engine = "comfyui"
    store.save(state)

    final = p.run(st.task_id, only_stage="tts", force=True)
    assert final.stages["tts"].status == StageStatus.DONE
    assert seen_engines == ["comfyui"], "任务级引擎覆盖未生效"
    assert fake_tts.voices and set(fake_tts.voices) == {"zh-CN-TestNeural"}, \
        f"音色未注入 synthesize: {fake_tts.voices}"
    assert final.stages["tts"].data["voice"] == "zh-CN-TestNeural"
    assert final.stages["tts"].data["engine"] == "fake"


def test_tts_cache_disabled_forces_resynth(config, video_with_srt, fake_llm, fake_tts):
    store = TaskStore(config)
    p, st = _prepare(store, config, video_with_srt, name="nocache")
    p.run(st.task_id, only_stage="tts", force=True)
    n1 = len(fake_tts.synthesized)
    assert n1 > 0

    state = store.load(st.task_id)
    state.project.tts_cache = False
    store.save(state)
    p.run(st.task_id, only_stage="tts", force=True)
    assert len(fake_tts.synthesized) > n1, "关闭缓存后应重新合成"


# ---------------------------------------------------------------------
# 双语字幕
# ---------------------------------------------------------------------
class FakeTranslator:
    """带 items 的翻译 prompt 返回 "EN " + 原文；其它 prompt 返回 {}。"""

    model = "fake-tr"

    def __init__(self):
        self.calls = 0

    def chat_json(self, messages, **kw):
        import json as _j

        user = next((m["content"] for m in messages if m["role"] == "user"), "")
        self.calls += 1
        try:
            data = _j.loads(user)
        except ValueError:
            return {}
        if isinstance(data, dict) and isinstance(data.get("items"), list):
            return {"items": [{"i": it["i"], "t": "EN " + it["t"]}
                              for it in data["items"]]}
        return {}


def test_subtitle_gen_bilingual(config, video_with_srt, fake_llm, fake_tts,
                                monkeypatch):
    from taggycat.media.sub import read_srt

    config.output.subtitle_bilingual = True
    config.output.subtitle_lang2 = "en"
    store = TaskStore(config)
    p, st = _prepare(store, config, video_with_srt, name="bi")
    tr = FakeTranslator()
    monkeypatch.setattr("taggycat.llm.get_llm", lambda cfg: tr)

    final = p.run(st.task_id, only_stage="tts", force=True)
    assert final.stages["tts"].status == StageStatus.DONE
    final = p.run(st.task_id, only_stage="subtitle_gen", force=True)
    assert final.stages["subtitle_gen"].status == StageStatus.DONE

    d = p.store.stage_dir(st.task_id, "subtitle_gen")
    mono = read_srt(d / "narration.srt")
    bi = read_srt(d / "narration.bilingual.srt")
    assert mono.cues and len(bi.cues) == len(mono.cues)
    for cm, cb in zip(mono.cues, bi.cues, strict=True):
        assert "\n" in cb.text, f"双语 cue 无换行: {cb.text!r}"
        zh, en = cb.text.split("\n", 1)
        assert zh == cm.text, "中文行应与单语一致"
        assert en.startswith("EN ")
    assert final.stages["subtitle_gen"].data["bilingual"] is True
    assert (d / "translations.json").exists()
    # 双语 ASS：译文行小字号（供烧录）
    ass = (d / "narration.bilingual.ass").read_text(encoding="utf-8")
    assert "Dialogue:" in ass and "{\\fs" in ass

    # 重跑：缓存命中，不再调 LLM
    n = tr.calls
    assert n > 0
    p.run(st.task_id, only_stage="subtitle_gen", force=True)
    assert tr.calls == n, "翻译缓存未命中"


def test_translate_cues_tolerant_shapes(monkeypatch):
    """翻译返回的别名键/字符串索引/扁平映射都能解析，缺项经兜底补齐。"""
    from types import SimpleNamespace

    from taggycat.pipeline.translate import translate_cues

    texts = ["他猛然抬头", "她推门而入", "警报响彻产房"]

    class TolerantLLM:
        model = "fake"

        def chat_json(self, messages, **kw):
            payload = json.loads(
                next(m["content"] for m in messages if m["role"] == "user"))
            items = payload["items"]
            if len(items) == 3:
                # 标准键 / 字符串 i + 别名键 / 第三条故意缺失
                return {"items": [
                    {"i": 0, "t": "He looks up"},
                    {"i": "1", "translation": "She walks in"},
                ]}
            return {str(items[0]["i"]): "Alarms blare"}   # 顶层扁平映射

    monkeypatch.setattr("taggycat.llm.get_llm",
                        lambda cfg: TolerantLLM())
    ctx = SimpleNamespace(config=None)
    res, errors = translate_cues(ctx, texts, "en", {})
    assert res == ["He looks up", "She walks in", "Alarms blare"]
    assert errors == []


def test_subtitle_gen_bilingual_llm_fail_degrades(config, video_with_srt,
                                                  fake_llm, fake_tts, monkeypatch):
    config.output.subtitle_bilingual = True
    store = TaskStore(config)
    p, st = _prepare(store, config, video_with_srt, name="bi-fail")

    class Boom:
        model = "boom"

        def chat_json(self, messages, **kw):
            raise RuntimeError("no valid api key")

    monkeypatch.setattr("taggycat.llm.get_llm", lambda cfg: Boom())
    p.run(st.task_id, only_stage="tts", force=True)
    final = p.run(st.task_id, only_stage="subtitle_gen", force=True)

    assert final.stages["subtitle_gen"].status == StageStatus.DONE
    d = p.store.stage_dir(st.task_id, "subtitle_gen")
    assert not (d / "narration.bilingual.srt").exists(), "失败不应留双语文件"
    assert (d / "narration.srt").exists()
    assert final.stages["subtitle_gen"].data["bilingual"] is False
    # 失败原因必须可见（否则静默降级难以排查）
    assert final.stages["subtitle_gen"].data["bilingual_error"]


def test_export_burns_bilingual_srt(config, video_with_srt, fake_llm, fake_tts,
                                    monkeypatch):
    config.output.subtitle_bilingual = True
    store = TaskStore(config)
    p, st = _prepare(store, config, video_with_srt, name="bi-export")
    monkeypatch.setattr("taggycat.llm.get_llm", lambda cfg: FakeTranslator())

    final = p.run(st.task_id)
    for s in ("tts", "subtitle_gen", "assemble", "export"):
        assert final.stages[s].status == StageStatus.DONE, \
            f"{s}: {final.stages[s].error}"
    data = final.stages["export"].data
    assert data["bilingual"] is True
    assert data["srt"].endswith("narration.bilingual.ass")
    assert Path(data["srt"]).exists()
    assert Path(data["output"]).exists()


def test_tts_pitch_match_wiring(config, video_with_srt, fake_llm, fake_tts):
    """音高对齐接线：素材与配音同为 440Hz → 差值≈0、不移调，data 记录状态。"""
    pytest.importorskip("numpy")
    config.output.voice_pitch_match = True
    store = TaskStore(config)
    p, st = _prepare(store, config, video_with_srt, name="pitch")

    final = p.run(st.task_id, only_stage="tts", force=True)
    assert final.stages["tts"].status == StageStatus.DONE
    data = final.stages["tts"].data
    assert data["pitch_shifted"] == 0          # 同频无须移调
    assert data["pitch_semitones"] is None
    assert data["pitch_range"] is None


# ---------------------------------------------------------------------
# 原声让路（voice_yield）：台词密集片段整段保留原声、不配音不出字幕
# ---------------------------------------------------------------------
def test_segment_cmd_keep_audio_passthrough(tmp_path):
    from types import SimpleNamespace

    from taggycat.config import OutputConfig
    from taggycat.models import Segment
    from taggycat.pipeline.stages_m4 import _segment_cmd

    seg = Segment(segment_id="s001", source_id="m1", start=0.0, clip_duration=5.0,
                  duration=5.0, audio_path="", keep_audio=True)
    o = OutputConfig(original_audio_mode="tail", voice_yield=True)
    ctx = SimpleNamespace(config=SimpleNamespace(output=o))
    cmd = " ".join(_segment_cmd(ctx, seg, tmp_path / "src.mp4", True,
                                tmp_path / "out.mp4"))
    # 整段原声直通：音量窗口/衰减都不加，且没有配音输入
    assert "[0:a]" in cmd
    assert "volume=" not in cmd
    assert "[1:a]" not in cmd and "amix" not in cmd


def test_mark_voice_yield_overlap(tmp_path):
    from types import SimpleNamespace

    from taggycat.config import OutputConfig
    from taggycat.models import Segment, StageName
    from taggycat.pipeline.stages_m4 import _mark_voice_yield

    asr_dir = tmp_path / StageName.ASR.value
    asr_dir.mkdir(parents=True, exist_ok=True)
    (asr_dir / "m1.json").write_text(json.dumps(
        {"language": "zh", "items": [{"start": 10.0, "end": 20.0, "text": "台词"}]}),
        encoding="utf-8")

    segs = [
        Segment(segment_id="s001", source_id="m1", start=10.0, end=14.0,
                plan_yield=True),
        Segment(segment_id="s002", source_id="m1", start=8.5, end=11.5),
        Segment(segment_id="s003", source_id="m1", start=0.0, end=4.0),
        Segment(segment_id="s004", source_id="m2", start=10.0, end=14.0),
    ]
    o = OutputConfig(voice_yield=True, voice_yield_min=2.0)
    ctx = SimpleNamespace(
        config=SimpleNamespace(output=o),
        stage_dir=lambda s: tmp_path / s.value,
        state=SimpleNamespace(sources=[SimpleNamespace(media_id="m1")]),
    )

    assert _mark_voice_yield(ctx, segs) == 1
    assert segs[0].keep_audio is True      # 重叠 4s ≥ max(2, 2)
    assert segs[1].keep_audio is False     # 重叠 1.5s < max(2, 1.5)=2
    assert segs[2].keep_audio is False     # 无重叠
    assert segs[3].keep_audio is False     # 无台词数据的素材

    # 开关关闭 → 全部不标记
    o.voice_yield = False
    assert _mark_voice_yield(ctx, segs) == 0
    assert not any(s.keep_audio for s in segs)

    # 无台词数据 → 不标记（退化为现状）
    (asr_dir / "m1.json").unlink()
    o.voice_yield = True
    assert _mark_voice_yield(ctx, segs) == 0


def test_mark_voice_yield_warns_on_bad_asr(tmp_path):
    """损坏的 ASR 数据不再静默：触发 warning。"""
    from types import SimpleNamespace

    from loguru import logger as _logger

    from taggycat.config import OutputConfig
    from taggycat.models import Segment, StageName
    from taggycat.pipeline.stages_m4 import _mark_voice_yield

    asr_dir = tmp_path / StageName.ASR.value
    asr_dir.mkdir(parents=True)
    (asr_dir / "m1.json").write_text("{坏json", encoding="utf-8")
    segs = [Segment(segment_id="s001", source_id="m1", start=1.0, end=2.0,
                    plan_yield=True)]
    ctx = SimpleNamespace(
        config=SimpleNamespace(output=OutputConfig(voice_yield=True)),
        stage_dir=lambda s: tmp_path / s.value,
        state=SimpleNamespace(sources=[SimpleNamespace(media_id="m1")]),
    )
    msgs: list[str] = []
    hid = _logger.add(msgs.append, level="WARNING")
    try:
        assert _mark_voice_yield(ctx, segs) == 0
    finally:
        _logger.remove(hid)
    assert any("ASR 数据解析失败" in m for m in msgs)


def test_load_trans_cache_warns_on_corrupt(tmp_path):
    """损坏的翻译缓存不再静默：触发 warning。"""
    from loguru import logger as _logger

    from taggycat.pipeline.translate import load_trans_cache

    (tmp_path / "translations.json").write_text("[坏", encoding="utf-8")
    msgs: list[str] = []
    hid = _logger.add(msgs.append, level="WARNING")
    try:
        assert load_trans_cache(tmp_path) == {}
    finally:
        _logger.remove(hid)
    assert any("翻译缓存损坏" in m for m in msgs)


def test_tts_voice_yield_skips_and_no_subtitle(config, video_with_srt,
                                               fake_llm, fake_tts, monkeypatch):
    config.output.voice_yield = True
    config.output.voice_yield_min = 2.0

    # plan 阶段点名全部片段让路（yield:1），满足双闸门之一
    class YieldLLM(FakeLLM):
        def chat_json(self, messages, **kw):
            r = super().chat_json(messages, **kw)
            if isinstance(r, dict) and "chosen" in r:
                for it in r["chosen"]:
                    it["yield"] = 1
            return r

    monkeypatch.setattr("taggycat.llm.get_llm", lambda cfg: YieldLLM())

    store = TaskStore(config)
    p, st = _prepare(store, config, video_with_srt, name="yield")

    # 覆写台词为全时段密集 → 所有片段让路
    asr_dir = p.store.stage_dir(st.task_id, "asr")
    for f in asr_dir.glob("*.json"):
        f.write_text(json.dumps(
            {"language": "zh",
             "items": [{"start": 0.0, "end": 999.0, "text": "密集台词覆盖全片"}]}),
            encoding="utf-8")

    final = p.run(st.task_id, only_stage="tts", force=True)
    assert final.stages["tts"].status == StageStatus.DONE
    data = final.stages["tts"].data
    assert data["voice_yield"] == data["segments"] > 0
    assert data["voice_total"] == 0
    assert fake_tts.synthesized == [], "让路片段不应合成配音"
    segs = json.loads(
        (p.store.stage_dir(st.task_id, "tts") / "segments.json").read_text(encoding="utf-8"))
    assert all(s["keep_audio"] and not s["audio_path"] for s in segs)

    final = p.run(st.task_id, only_stage="subtitle_gen", force=True)
    assert final.stages["subtitle_gen"].status == StageStatus.DONE
    assert final.stages["subtitle_gen"].data["cues"] == 0, "让路时段不应出口播字幕"
    assert not (p.store.stage_dir(st.task_id, "subtitle_gen")
                / "narration.bilingual.srt").exists()

    # 台词稀疏（删掉台词）→ 重跑恢复讲解
    for f in asr_dir.glob("*.json"):
        f.write_text(json.dumps({"language": "zh", "items": []}), encoding="utf-8")
    final = p.run(st.task_id, only_stage="tts", force=True)
    assert final.stages["tts"].data["voice_yield"] == 0
    assert len(fake_tts.synthesized) > 0
