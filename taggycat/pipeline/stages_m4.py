"""M4 阶段：逐段配音 → 字幕时间轴 → FFmpeg 剪辑合成 → 成片导出。

配音处理见 taggycat.pipeline.voice，字幕翻译见 taggycat.pipeline.translate，
字幕切分工具见 taggycat.media.sub；本模块保留阶段函数并 re-export 旧名称。
"""

from __future__ import annotations

import json
import os
import shutil
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from loguru import logger

from taggycat.media.ffmpeg import (
    FFmpegError,
    encode_args,
    ffmpeg_bin,
    load_probe,
    media_duration,
    probe,
    run_command,
)
from taggycat.media.sub import (
    CAPTION_PUNCT,
    CUE_MAX_CHARS,
    Cue,
    Subtitle,
    split_caption,
    strip_caption_punct,
    write_bilingual_ass,
    write_srt,
)
from taggycat.models import Segment, StageName
from taggycat.pipeline.orchestrator import StageContext, StageSpec
from taggycat.pipeline.segments_io import load_segments, write_segments
from taggycat.pipeline.translate import (
    TRANS_CACHE_FILE,
    ass_font_name,
    lang_name,
    load_trans_cache,
    translate_cues,
)
from taggycat.pipeline.voice import (
    TAIL_GAP,
    align_pitch,
    extend_for_voice,
    load_source_bounds,
    mark_voice_yield,
)

# 旧名称 re-export（兼容 from stages_m4 import _xxx 路径）
_ass_font_name = ass_font_name
_lang_name = lang_name
_load_trans_cache = load_trans_cache
_translate_cues = translate_cues
_align_pitch = align_pitch
_extend_for_voice = extend_for_voice
_load_source_bounds = load_source_bounds
_mark_voice_yield = mark_voice_yield

__all__ = [
    "CAPTION_PUNCT", "CUE_MAX_CHARS", "TAIL_GAP", "TRANS_CACHE_FILE",
    "split_caption", "strip_caption_punct",
    "offset_of", "register_m4",
    "run_tts", "run_subtitle_gen", "run_assemble", "run_export",
    "_segment_cmd", "_load_final_segments",
    "_ass_font_name", "_lang_name", "_load_trans_cache", "_translate_cues",
    "_align_pitch", "_extend_for_voice", "_load_source_bounds", "_mark_voice_yield",
]


# ---------------------------------------------------------------------
# 通用：读取已审核文案
# ---------------------------------------------------------------------
def _load_final_segments(ctx: StageContext, include_audio: bool = False) -> list[Segment]:
    """按 match → script → plan 顺序取最终文案。

    include_audio=True 时优先读 tts/segments.json（含配音路径与延长时长），
    供 subtitle_gen / assemble 使用；否则取最新文案（供 tts 自身）。
    """
    order = [StageName.TTS, StageName.MATCH, StageName.SCRIPT, StageName.PLAN] \
        if include_audio else [StageName.MATCH, StageName.SCRIPT, StageName.PLAN]
    for stage in order:
        f = ctx.stage_dir(stage) / "segments.json"
        if f.exists():
            return load_segments(f)
    raise RuntimeError("segments.json 不存在，请先执行 plan/script/match 阶段")


def _save_final_segments(ctx: StageContext, segs: list[Segment], stage: StageName) -> Path:
    return write_segments(ctx.stage_dir(stage) / "segments.json", segs)


# ---------------------------------------------------------------------
# 11. tts：逐段配音（带缓存）
# ---------------------------------------------------------------------
def _effective_edge_rate(project, configured: str, style) -> str:
    """任务实际使用的 edge 语速：任务覆盖 > 全局非默认 > 风格推荐语速。"""
    from taggycat.prompts import style_edge_rate

    if project.rate and project.rate.strip():
        return project.rate.strip()
    c = (configured or "").strip()
    if c and c not in ("+0%", "+0", "0%"):
        return c
    return style_edge_rate(style)


def run_tts(ctx: StageContext) -> dict:
    from taggycat.prompts import get_style
    from taggycat.tts import cache_key, cached_audio, get_tts

    segs = _load_final_segments(ctx)
    cfg = ctx.config
    project = ctx.state.project

    # 原声让路：台词密集片段整段保留原声，不合成配音
    yield_n = _mark_voice_yield(ctx, segs)

    # 任务级引擎覆盖（UI/CLI 选择）不污染全局配置
    if project.tts_engine:
        cfg = cfg.model_copy(deep=True)
        cfg.tts.engine = project.tts_engine

    # 风格语速 → edge 配音语速（与 m3 的字数预算同源，保证配音时长与文案同步）
    if (cfg.tts.engine or "edge").lower() == "edge":
        eff = _effective_edge_rate(project, cfg.tts.edge.rate, get_style(project.style))
        if eff and eff != cfg.tts.edge.rate:
            cfg = cfg.model_copy(deep=True)
            cfg.tts.edge.rate = eff

    engine = get_tts(cfg)

    voice = project.voice or cfg.tts.default_voice or cfg.tts.edge.voice
    if engine.name.startswith("comfyui"):
        # ComfyUI 音色由工作流/参考音频决定：纳入缓存键，换工作流或参考音频不复用旧音频
        clone = engine.name == "comfyui_clone"
        cc = cfg.tts.comfyui_clone if clone else cfg.tts.comfyui
        voice = f"{cc.workflow}|{cc.reference_audio if clone else ''}"
    rate = cfg.tts.edge.rate if engine.name == "edge" else ""
    use_cache = project.tts_cache
    cache_dir = cfg.resolve(cfg.paths.tts_cache)
    cache_dir.mkdir(parents=True, exist_ok=True)

    hits = 0
    total_voice = 0.0
    overshoot: list[float] = []

    def apply_audio(s: Segment, audio: Path) -> None:
        """回填一段配音结果（在合成/命中之后统一调用）。"""
        nonlocal total_voice
        s.audio_path = str(audio)
        s.voice_duration = round(media_duration(audio), 3)
        s.duration = round(max(s.clip_duration, s.voice_duration + TAIL_GAP), 3)
        total_voice += s.voice_duration
        if s.clip_duration > 0 and s.voice_duration > s.clip_duration * 1.15:
            overshoot.append(
                round((s.voice_duration - s.clip_duration) / s.clip_duration, 2)
            )

    # 顺序处理：让路/空文本直接定形，缓存命中直接回填；未命中段进并发池
    pending: list[tuple[int, Segment, str, str]] = []
    for i, s in enumerate(segs, 1):
        if s.keep_audio:
            s.audio_path = ""
            s.voice_duration = 0.0
            s.duration = s.clip_duration
            logger.info("  [{}/{}] {} 原声片段，跳过配音", i, len(segs), s.segment_id)
            continue
        text = (s.caption or "").strip()
        if not text:
            s.duration = s.clip_duration
            continue

        key = cache_key(engine.name, voice, rate, text)
        audio = cached_audio(cache_dir, key, engine.ext) if use_cache else None
        if audio:
            hits += 1
        else:
            logger.info("  [{}/{}] 合成 {}（{} 字）", i, len(segs), s.segment_id, len(text))
            pending.append((i, s, text, key))
            continue
        apply_audio(s, audio)

    # 未命中段：有界并发合成，结果按序回填（延伸/音高对齐仍在之后串行）
    if pending:
        workers = max(1, min(cfg.tts.max_workers, len(pending)))
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = [
                (s, ex.submit(engine.synthesize, text, cache_dir / key, voice=voice))
                for _i, s, text, key in pending
            ]
        for s, fut in futures:
            apply_audio(s, fut.result())

    # 消除画面定格：配音超长的片段向后延伸出点（受相邻片段/源片边界钳制）
    ext_stats = _extend_for_voice(segs, *_load_source_bounds(ctx))
    if ext_stats["segments"]:
        logger.info(
            "片段延伸: {} 段共 +{:.1f}s（配音补足画面，消除定格）",
            ext_stats["segments"], ext_stats["seconds"],
        )
    if ext_stats["partial"]:
        logger.warning(
            "{} 段受相邻片段/源片边界限制无法完全延伸，仍会短时画面定格",
            ext_stats["partial"],
        )
    if overshoot:
        logger.warning(
            "配音超片段时长 >15%: {} 段（最大 +{:.0%}）；可缩短文案或调低语速",
            len(overshoot), max(overshoot),
        )

    # 音高对齐：逐段向各自原声靠拢（时长不变；让路段无配音不处理）
    voiced = [s for s in segs if s.audio_path and not s.keep_audio]
    shifted: list[float] = []
    if voiced:
        shifted = _align_pitch(ctx, segs)
        if shifted:
            sv = sorted(shifted)
            med = sv[len(sv) // 2]
            total_voice = sum(s.voice_duration for s in segs if s.audio_path)
            logger.info(
                "音高对齐: {}/{} 段移调，中位 {:+.1f} 半音（范围 {:+.1f}~{:+.1f}）",
                len(shifted), len(voiced), med, sv[0], sv[-1],
            )
        else:
            logger.info("音高对齐: 各段差值可忽略或估计失败，配音保持原音高")

    out = _save_final_segments(ctx, segs, StageName.TTS)
    total_out = sum(s.duration for s in segs)
    logger.info(
        "配音完成: {} 段, 台词 {:.1f}s, 成片片段总长 {:.1f}s（缓存命中 {}）",
        len(segs), total_voice, total_out, hits,
    )
    ctx.state.stages[StageName.TTS.value].data = {
        "segments": len(segs),
        "voice_total": round(total_voice, 1),
        "output_total": round(total_out, 1),
        "cache_hits": hits,
        "voice_yield": yield_n,
        "extend_segments": ext_stats["segments"],
        "extend_seconds": round(ext_stats["seconds"], 1),
        "freeze_partial": ext_stats["partial"],
        "overshoot_segments": len(overshoot),
        "overshoot_max": max(overshoot) if overshoot else None,
        "pitch_shifted": len(shifted),
        "pitch_semitones": (
            round(sorted(shifted)[len(shifted) // 2], 2) if shifted else None
        ),
        "pitch_range": (
            [round(min(shifted), 2), round(max(shifted), 2)] if shifted else None
        ),
        "engine": engine.name,
        "voice": voice,
        "cache": use_cache,
    }
    return {"segments": out}


# ---------------------------------------------------------------------
# 12. subtitle_gen：口播字幕时间轴
# ---------------------------------------------------------------------
def run_subtitle_gen(ctx: StageContext) -> dict:
    out_dir = ctx.stage_dir(StageName.SUBTITLE_GEN)
    segs = _load_final_segments(ctx, include_audio=True)

    cues: list[Cue] = []
    offset = 0.0
    for s in segs:
        span = s.duration or s.clip_duration
        if s.keep_audio:
            # 原声让路片段：整段原声呈现，不出口播字幕（该时段不讲解）
            offset += span
            continue
        voice_span = s.voice_duration or span
        text = (s.caption or "").strip()
        if text:
            chunks = split_caption(text)
            total_chars = sum(len(c) for c in chunks) or 1
            t = offset
            for c in chunks:
                d = max(0.4, voice_span * len(c) / total_chars)
                d = min(d, offset + span - t)
                if d <= 0:
                    break
                cues.append(
                    Cue(index=len(cues) + 1, start=t, end=round(t + d, 3), text=c)
                )
                t += d
        offset += span

    sub = Subtitle(cues=cues)
    srt_path = write_srt(sub, out_dir / "narration.srt")

    # 双语：翻译后另存 narration.bilingual.srt（烧录时选用；全失败回退单语）
    o = ctx.config.output
    bilingual = False
    translated = 0
    tr_errors: list[str] = []
    if o.subtitle_bilingual and cues:
        bi_path = out_dir / "narration.bilingual.srt"
        bi_path.unlink(missing_ok=True)          # 旧文件作废，防止文案变更后烧过期译文
        cache = _load_trans_cache(out_dir)
        trans, tr_errors = _translate_cues(ctx, [c.text for c in cues],
                                           o.subtitle_lang2 or "en", cache)
        (out_dir / TRANS_CACHE_FILE).write_text(
            json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        translated = sum(1 for t in trans if t)
        if translated:
            bi = Subtitle(cues=[
                Cue(index=c.index, start=c.start, end=c.end,
                    text=f"{c.text}\n{t}" if t else c.text)
                for c, t in zip(cues, trans, strict=True)
            ])
            write_srt(bi, bi_path)
            write_bilingual_ass(
                bi, out_dir / "narration.bilingual.ass",
                width=o.width, height=o.height, font_size=o.subtitle_fontsize,
                font_name=_ass_font_name(ctx.config.resolve(o.subtitle_font)),
            )
            bilingual = True
            logger.info("双语字幕: {}/{} 条已翻译（{}）", translated, len(cues),
                        _lang_name(o.subtitle_lang2 or "en"))
        else:
            logger.warning("双语字幕翻译全部失败，本任务回退单语")

    timeline = {
        "total_duration": round(offset, 3),
        "cues": len(cues),
        "segments": [
            {"id": s.segment_id, "start": round(offset_of(segs, s), 3),
             "duration": s.duration or s.clip_duration}
            for s in segs
        ],
    }
    (out_dir / "timeline.json").write_text(
        json.dumps(timeline, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    logger.info("字幕生成: {} 条, 成片总长 {:.1f}s", len(cues), offset)
    ctx.state.stages[StageName.SUBTITLE_GEN.value].data = {
        "cues": len(cues),
        "total_duration": round(offset, 1),
        "bilingual": bilingual,
        "translated": translated,
        "bilingual_error": "; ".join(tr_errors)[:300] if tr_errors else "",
    }
    if tr_errors and not bilingual:
        logger.warning("双语字幕翻译失败（{}），本任务字幕为单语；重跑 subtitle_gen 可重试",
                       "; ".join(tr_errors)[:200])
    return {"srt": srt_path}


def offset_of(segs: list[Segment], target: Segment) -> float:
    t = 0.0
    for s in segs:
        if s.segment_id == target.segment_id:
            return t
        t += s.duration or s.clip_duration
    return 0.0


# ---------------------------------------------------------------------
# 13. assemble：逐片段裁剪/配音/混合，再拼接
# ---------------------------------------------------------------------
def _segment_cmd(
    ctx: StageContext, seg: Segment, src: Path, has_audio: bool, out: Path
) -> list[str]:
    """构造单片段 ffmpeg 命令（统一编码参数，便于 concat -c copy）。"""
    o = ctx.config.output
    clip_dur = seg.clip_duration
    seg_dur = seg.duration or clip_dur
    ext = max(0.0, seg_dur - clip_dur)

    vchain = (
        f"scale={o.width}:{o.height}:force_original_aspect_ratio=decrease,"
        f"pad={o.width}:{o.height}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={o.fps}"
    )
    if ext > 0.01:
        vchain += f",tpad=stop_mode=clone:stop_duration={ext:.3f}"

    keep_seg = getattr(seg, "keep_audio", False)
    # 讲解段遮盖原片烧录字幕（原声让路段保留原字幕与原声）
    video_stmt = f"[0:v]{vchain}[vout]"
    if o.hard_sub_remove and not keep_seg:
        rect = o.hard_sub_rect()
        if rect:
            x, y, w, h = rect
            if o.effective_hard_sub_mode == "delogo":
                video_stmt = (
                    f"[0:v]{vchain},delogo=x={x}:y={y}:w={w}:h={h}[vout]"
                )
            else:
                # 高斯模糊遮盖带：比 delogo 插值抹除更自然，原字幕不可辨认
                # FFmpeg 8 限制 boxblur 半径：luma ≤18、chroma ≤9（yuv420p 子采样）
                br = min(18, max(1, min(w, h) // 2))
                cr = min(9, max(1, br // 2))
                video_stmt = (
                    f"[0:v]{vchain},split=2[btb][btf];"
                    f"[btf]crop={w}:{h}:{x}:{y},"
                    f"boxblur=luma_radius={br}:luma_power=3"
                    f":chroma_radius={cr}:chroma_power=3[bbl];"
                    f"[btb][bbl]overlay={x}:{y}[vout]"
                )

    aformat = (
        "aresample=44100,aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo,"
        f"apad=whole_dur={seg_dur:.3f},atrim=0:{seg_dur:.3f}"
    )
    voice_path = Path(seg.audio_path) if seg.audio_path else None
    use_voice = bool(voice_path and voice_path.exists())
    # 原声让路片段：整段原声、音量直通（完整呈现原声，该时段无讲解）
    mode = "keep" if keep_seg else o.effective_original_audio   # keep | mute | tail
    use_orig = has_audio and mode in ("keep", "tail")

    parts: list[str] = []
    labels: list[str] = []
    if use_orig:
        if keep_seg:
            parts.append(f"[0:a]{aformat}[a1]")
        else:
            orig = f"[0:a]{aformat}"
            if mode == "tail":
                # 仅段尾一小段放原声（volume 窗口开关；不能用 adelay——会写坏音轨 stts，
                # 导致 concat -c copy 后元数据异常）
                tail = max(0.1, min(o.original_audio_tail, seg_dur))
                start = max(0.0, seg_dur - tail)
                orig += (f",volume={o.original_audio_volume}"
                         f":enable='between(t,{start:.3f},{seg_dur:.3f})'")
            else:
                orig += f",volume={o.original_audio_volume}"
            parts.append(orig + "[a1]")
        labels.append("a1")
    if use_voice:
        # 输入顺序固定：0=视频源, 1=配音
        parts.append(f"[1:a]{aformat}[a2]")
        labels.append("a2")

    if len(labels) == 2:
        parts.append("[a1][a2]amix=inputs=2:duration=longest:normalize=0[aout]")
    elif len(labels) == 1:
        parts.append(f"[{labels[0]}]anull[aout]")
    else:
        parts.append(
            f"anullsrc=channel_layout=stereo:sample_rate=44100,"
            f"atrim=0:{seg_dur:.3f}[aout]"
        )

    cmd = ["-hide_banner", "-y"]
    if use_voice:
        cmd += ["-ss", f"{seg.start:.3f}", "-t", f"{clip_dur:.3f}", "-i", str(src),
                "-i", str(voice_path)]
    else:
        cmd += ["-ss", f"{seg.start:.3f}", "-t", f"{clip_dur:.3f}", "-i", str(src)]

    cmd += [
        "-filter_complex",
        ";".join([video_stmt] + parts),
        "-map", "[vout]", "-map", "[aout]",
        "-t", f"{seg_dur:.3f}",
        *encode_args(o),
        "-ar", "44100", "-ac", "2",
        str(out),
    ]
    return cmd


def run_assemble(ctx: StageContext) -> dict:
    out_dir = ctx.stage_dir(StageName.ASSEMBLE)
    segs = _load_final_segments(ctx, include_audio=True)

    # 各素材是否有原声
    has_audio_map: dict[str, bool] = {}
    for src in ctx.state.sources:
        pf = ctx.stage_dir(StageName.PROBE) / f"{src.media_id}.json"
        info = load_probe(pf)
        has_audio_map[src.media_id] = (
            info.has_audio if info is not None else probe(src.path).has_audio
        )

    path_map = {s.media_id: Path(s.path) for s in ctx.state.sources}
    jobs: list[tuple[int, Segment, Path, list[str]]] = []
    for i, seg in enumerate(segs, 1):
        src = path_map.get(seg.source_id)
        if not src or not src.exists():
            raise FileNotFoundError(f"素材不存在: {seg.source_id}")
        out = out_dir / f"seg_{i:03d}.mp4"
        cmd = _segment_cmd(ctx, seg, src, has_audio_map.get(seg.source_id, False), out)
        logger.info("  [{}/{}] 合成 {}（{:.1f}s）", i, len(segs), seg.segment_id,
                    seg.duration or seg.clip_duration)
        jobs.append((i, seg, out, cmd))

    # 段级有界并行：≤ min(4, 物理核/2)；产物 seg_*.mp4 齐全后再 concat
    workers = max(1, min(4, (os.cpu_count() or 2) // 2, len(jobs)))
    parts: list[Path | None] = [None] * len(jobs)
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = [
            (i, seg, out, ex.submit(run_command, [ffmpeg_bin(), *cmd], timeout=1800))
            for i, seg, out, cmd in jobs
        ]
    # 按序结算（失败时指明对应段）
    for i, seg, out, fut in futures:
        try:
            fut.result()
        except FFmpegError as e:
            raise RuntimeError(f"片段 {seg.segment_id} 合成失败: {e}") from e
        parts[i - 1] = out

    # 拼接
    if any(p is None for p in parts):
        raise RuntimeError("部分片段产物缺失，无法拼接")
    final_parts: list[Path] = [p for p in parts if p is not None]
    base = out_dir / "base.mp4"
    if len(final_parts) == 1:
        shutil.copyfile(final_parts[0], base)
    else:
        lst = out_dir / "concat.txt"
        lst.write_text(
            "\n".join(f"file '{p.name}'" for p in final_parts) + "\n", encoding="utf-8"
        )
        run_command([
            ffmpeg_bin(), "-hide_banner", "-y",
            "-f", "concat", "-safe", "0", "-i", str(lst),
            "-c", "copy", str(base),
        ], timeout=1800)

    total = sum(s.duration or s.clip_duration for s in segs)
    logger.info("剪辑合成完成: {} 段 -> base.mp4 ({:.1f}s)", len(final_parts), total)
    ctx.state.stages[StageName.ASSEMBLE.value].data = {
        "parts": len(final_parts),
        "total_duration": round(total, 1),
    }
    return {"base": base}


# ---------------------------------------------------------------------
# 14. export：BGM 混音 + 字幕烧录 + 导出
# ---------------------------------------------------------------------
def _escape_sub_path(p: Path) -> str:
    s = str(p).replace("\\", "/").replace(":", r"\:").replace("'", r"\'")
    return s


def run_export(ctx: StageContext) -> dict:
    cfg = ctx.config
    o = cfg.output
    out_dir = ctx.stage_dir(StageName.ASSEMBLE)
    base = out_dir / "base.mp4"
    if not base.exists():
        raise RuntimeError("base.mp4 不存在，请先执行 assemble 阶段")

    project = ctx.state.project
    dest_dir = cfg.resolve(project.output_dir) / ctx.state.task_id
    dest_dir.mkdir(parents=True, exist_ok=True)
    safe_name = "".join(c for c in (ctx.state.name or "成片") if c not in '\\/:*?"<>|')
    dest = dest_dir / f"{safe_name}.mp4"

    # BGM
    bgm: Path | None = None
    if project.bgm:
        bgm = cfg.resolve(project.bgm)
        if not bgm.exists():
            logger.warning("背景音乐不存在，忽略: {}", bgm)
            bgm = None

    # 字幕：双语优先烧录 ASS（译文行小字号），否则双语 SRT / 单语 SRT
    srt_dir = ctx.stage_dir(StageName.SUBTITLE_GEN)
    srt_mono = srt_dir / "narration.srt"
    srt_bi = srt_dir / "narration.bilingual.srt"
    srt_bi_ass = srt_dir / "narration.bilingual.ass"
    bi_ok = bool(o.subtitle_bilingual and srt_bi.exists()
                 and srt_bi.stat().st_size > 0)
    ass_ok = bi_ok and srt_bi_ass.exists() and srt_bi_ass.stat().st_size > 0
    bilingual = bi_ok
    srt = srt_bi_ass if ass_ok else (srt_bi if bi_ok else srt_mono)
    burn = o.subtitle and srt.exists() and srt.stat().st_size > 0

    cmd = [ffmpeg_bin(), "-hide_banner", "-y", "-i", str(base)]
    if bgm:
        cmd += ["-stream_loop", "-1", "-i", str(bgm)]

    filters: list[str] = []
    if burn:
        sub = f"subtitles=filename='{_escape_sub_path(srt)}'"
        font = cfg.resolve(o.subtitle_font)
        if font.exists():
            sub += f":fontsdir='{_escape_sub_path(font.parent)}'"
        if not str(srt).endswith(".ass"):
            # SRT 无样式，字号靠 force_style；ASS 自带样式（译文行缩小），不覆盖
            sub += f":force_style='FontSize={o.subtitle_fontsize}'"
        filters.append(f"[0:v]{sub}[vout]")

    audio_label = "0:a"
    if bgm:
        bg_lbl = f"bg{len(filters)}"
        filters.append(
            f"[1:a]volume={o.bgm_volume}[{bg_lbl}]"
        )
        filters.append(
            f"[0:a][{bg_lbl}]amix=inputs=2:duration=first:normalize=0[aout]"
        )
        audio_label = "[aout]"

    if filters:
        cmd += ["-filter_complex", ";".join(filters)]
    if burn:
        cmd += ["-map", "[vout]"]
    else:
        cmd += ["-map", "0:v"]
    cmd += ["-map", audio_label]

    reencode_v = burn
    if reencode_v:
        cmd += encode_args(o)
    else:
        cmd += ["-c:v", "copy"]
        cmd += (
            ["-c:a", o.audio_codec, "-b:a", o.audio_bitrate]
            if bgm else ["-c:a", "copy"]
        )
    cmd += ["-movflags", "+faststart", str(dest)]

    logger.info("导出成片: {}", dest)
    run_command(cmd, timeout=3600)

    size_mb = dest.stat().st_size / (1024 * 1024)
    dur = media_duration(dest)
    logger.info("成片完成: {}（{:.1f}s, {:.1f} MB）", dest, dur, size_mb)
    ctx.state.stages[StageName.EXPORT.value].data = {
        "output": str(dest),
        "duration": round(dur, 1),
        "size_mb": round(size_mb, 1),
        "subtitle_burned": burn,
        "bilingual": bilingual and burn,
        "srt": str(srt),
        "bgm": bool(bgm),
    }

    # 中间产物清理：删段级 seg_*.mp4 与音高移调 wav，保留 base.mp4（供重导出）
    for seg_part in out_dir.glob("seg_*.mp4"):
        seg_part.unlink(missing_ok=True)
    tts_cache_dir = cfg.resolve(cfg.paths.tts_cache)
    for pitch_wav in tts_cache_dir.glob("*_p*.wav"):
        pitch_wav.unlink(missing_ok=True)
    return {"output": dest}


def register_m4(p) -> None:
    p.register(StageSpec(StageName.TTS, run_tts, "逐段配音（edge/ComfyUI）"))
    p.register(StageSpec(StageName.SUBTITLE_GEN, run_subtitle_gen, "口播字幕时间轴"))
    p.register(StageSpec(StageName.ASSEMBLE, run_assemble, "FFmpeg 逐段合成与拼接"))
    p.register(StageSpec(StageName.EXPORT, run_export, "BGM 混音 + 字幕烧录 + 导出"))
