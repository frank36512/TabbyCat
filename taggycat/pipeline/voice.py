"""配音相关处理：原声让路、音高对齐、片段延伸钳制。"""

from __future__ import annotations

import json
from pathlib import Path

from loguru import logger

from taggycat.media.ffmpeg import load_probe, media_duration
from taggycat.models import Segment, StageName
from taggycat.pipeline.orchestrator import StageContext

# 片段尾部留白（解说念完后的呼吸位）
TAIL_GAP = 0.4


def mark_voice_yield(ctx: StageContext, segs: list[Segment]) -> int:
    """标记整段保留原声的片段（该时段不讲解）——为讲解服务，不随便露出。

    双闸门：
      1) 规划点名 plan_yield：plan 阶段 LLM 标记"原声台词完整露出以配合解说"的片段；
      2) 台词重叠 ≥ max(voice_yield_min, 片段时长一半)（安全下限）。
    两者同时满足才让路；未被点名的片段即使台词密集也不让路，防止原声原字幕
    随便露出打断讲解。无台词数据时全部不标记（行为与未开启一致）。
    """
    for s in segs:
        s.keep_audio = False
    o = ctx.config.output
    if not o.voice_yield:
        return 0
    if not any(s.plan_yield for s in segs):
        return 0
    threshold = max(0.0, o.voice_yield_min)
    for src in ctx.state.sources:
        f = ctx.stage_dir(StageName.ASR) / f"{src.media_id}.json"
        if not f.exists():
            continue
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            logger.warning("ASR 数据解析失败 {}: {}", f, e)
            continue
        items = data if isinstance(data, list) else (data.get("items") or [])
        spans: list[tuple[float, float]] = []
        for it in items:
            if not isinstance(it, dict):
                continue
            try:
                spans.append((float(it["start"]), float(it["end"])))
            except (KeyError, TypeError, ValueError):
                continue
        if not spans:
            continue
        for s in segs:
            if s.source_id != src.media_id or not s.plan_yield:
                continue
            clip = s.clip_duration
            if clip <= 0:
                continue
            a, b = s.start, s.start + clip
            overlap = sum(max(0.0, min(b, i1) - max(a, i0)) for i0, i1 in spans)
            if overlap >= max(threshold, clip * 0.5):
                s.keep_audio = True
    return sum(1 for s in segs if s.keep_audio)


def align_pitch(ctx: StageContext, segs: list[Segment]) -> list[float]:
    """段级音高对齐：逐段估计「该段原声 vs 该段配音」基频差并移调配音（时长不变）。

    每段独立向自己的原声靠拢（避免任务级统一移调造成有的段过冲、有的不到位）。
    返回实际应用的半音列表（|差|<0.25 的段不移调、不计入）。
    """
    from taggycat.media.pitch import apply_pitch, f0_of_audio, semitones_between

    o = ctx.config.output
    if not o.voice_pitch_match:
        return []
    src_path = {s.media_id: Path(s.path) for s in ctx.state.sources}
    limit = max(0.0, o.voice_pitch_max_semitones)
    applied: list[float] = []
    for s in segs:
        if s.keep_audio or not s.audio_path:
            continue
        f_voice = f0_of_audio(s.audio_path)
        src = src_path.get(s.source_id)
        if not f_voice or src is None or not src.exists():
            continue
        f_orig = f0_of_audio(src, start=s.start, dur=s.clip_duration)
        semi = semitones_between(f_orig, f_voice)
        if semi is None:
            continue
        if abs(semi) < 0.25:
            logger.info("  {} 音高差 {:+.1f} 半音，无需移调", s.segment_id, semi)
            continue
        c = max(-limit, min(limit, semi))
        newp = apply_pitch(Path(s.audio_path), c)
        if newp is None:
            logger.warning("  {} 移调 {:+.1f} 失败，保留原配音", s.segment_id, c)
            continue
        s.audio_path = str(newp)
        s.voice_duration = round(media_duration(newp), 3)
        s.duration = round(max(s.clip_duration, s.voice_duration + TAIL_GAP), 3)
        applied.append(c)
        resid = semitones_between(f_orig, f0_of_audio(newp))
        logger.info(
            "  {} 音高对齐 {:+.1f} 半音（原声 {:.0f}Hz ↔ 配音 {:.0f}Hz，残差 {:+.1f}）",
            s.segment_id, c, f_orig or 0.0, f_voice, resid if resid is not None else 0.0,
        )
        if resid is not None and abs(resid) > 2.0:
            logger.warning(
                "  {} 移调后仍差 {:+.1f} 半音（估计误差或超 ±{:.1f} 上限）",
                s.segment_id, resid, limit,
            )
    return applied


def load_source_bounds(ctx: StageContext) -> tuple[dict[str, float], dict[str, list[float]]]:
    """各源片时长与场景切点（供片段延伸钳制）。"""
    durations: dict[str, float] = {}
    cuts: dict[str, list[float]] = {}
    for src in ctx.state.sources:
        pf = ctx.stage_dir(StageName.PROBE) / f"{src.media_id}.json"
        info = load_probe(pf)
        if info is not None:
            durations[src.media_id] = info.duration
        elif pf.exists():
            logger.warning("probe 数据解析失败: {}", pf)
        cf = ctx.stage_dir(StageName.FRAMES) / src.media_id / "scenes.json"
        lst: list[float] = []
        if cf.exists():
            try:
                for c in json.loads(cf.read_text(encoding="utf-8")):
                    t = float(c.get("time_sec", 0))
                    if t > 0:
                        lst.append(t)
            except (OSError, ValueError, TypeError) as e:
                logger.warning("场景切点解析失败 {}: {}", cf, e)
                lst = []
        cuts[src.media_id] = lst
    return durations, cuts


def extend_for_voice(
    segs: list[Segment],
    source_durations: dict[str, float],
    source_cuts: dict[str, list[float]],
) -> dict:
    """配音超长时向后延伸片段出点，消除画面定格（tpad 仅作兜底）。

    约束：同源不越过下一个选中片段的入点、不超源片末尾；
    延伸量富余时优先停在场景切点（+1.5s 内）。
    返回 {"segments": 延伸段数, "seconds": 延伸总秒, "partial": 仍会定格的段数}。
    """
    stats = {"segments": 0, "seconds": 0.0, "partial": 0}
    by_source: dict[str, list[Segment]] = {}
    for s in segs:
        by_source.setdefault(s.source_id, []).append(s)

    for s in segs:
        if s.keep_audio or not s.audio_path:
            continue
        need = (s.voice_duration + TAIL_GAP) - s.clip_duration
        if need <= 0.05:
            continue
        group = by_source.get(s.source_id, [])
        nxt = min((o.start for o in group if o.start > s.start + 1e-6), default=None)
        limit = source_durations.get(s.source_id, float("inf"))
        if nxt is not None:
            limit = min(limit, nxt)
        take = min(need, max(0.0, limit - s.end))
        if take <= 0:
            stats["partial"] += 1
            continue
        old_end = s.end
        new_end = s.end + take
        for c in sorted(source_cuts.get(s.source_id, [])):
            if new_end < c <= new_end + 1.5:
                new_end = min(c, limit)
                break
        s.end = round(new_end, 3)
        s.duration = round(max(s.clip_duration, s.voice_duration + TAIL_GAP), 3)
        stats["segments"] += 1
        stats["seconds"] += round(s.end - old_end, 2)
        if s.clip_duration + 0.05 < s.voice_duration + TAIL_GAP:
            stats["partial"] += 1
    return stats


# 旧名称兼容
_mark_voice_yield = mark_voice_yield
_align_pitch = align_pitch
_load_source_bounds = load_source_bounds
_extend_for_voice = extend_for_voice
