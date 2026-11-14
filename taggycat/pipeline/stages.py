"""已实现的流水线阶段（M1：probe / subtitle / frames）。"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from loguru import logger

from taggycat.media import (
    detect_scenes_with_frames,
    extract_frames,
    probe,
    read_srt,
)
from taggycat.models import MediaInfo, StageName
from taggycat.pipeline.orchestrator import Pipeline, StageContext, StageSpec


def _saved_probe(ctx: StageContext, media_id: str) -> MediaInfo | None:
    """读取 probe 阶段落盘信息（缺失返回 None）。"""
    f = ctx.stage_dir(StageName.PROBE) / f"{media_id}.json"
    if f.exists():
        return MediaInfo.model_validate_json(f.read_text(encoding="utf-8"))
    return None


# ---------------------------------------------------------------------
# 1. probe：媒体探测
# ---------------------------------------------------------------------
def run_probe(ctx: StageContext) -> dict:
    out_dir = ctx.stage_dir(StageName.PROBE)
    infos: list[MediaInfo] = []
    for src in ctx.state.sources:
        info = probe(src.path)
        infos.append(info)
        (out_dir / f"{src.media_id}.json").write_text(
            info.model_dump_json(indent=2), encoding="utf-8"
        )
        logger.info(
            "  {} | {:.1f}s | {}x{} @ {:.2f}fps | audio={} | subs={}",
            src.name, info.duration, info.width, info.height,
            info.fps, info.has_audio, len(info.subtitle_streams),
        )

    total_dur = sum(i.duration for i in infos)
    summary = {
        "count": len(infos),
        "total_duration_sec": round(total_dur, 2),
        "total_size_mb": round(sum(s.size_mb for s in ctx.state.sources), 2),
    }
    (out_dir / "_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    ctx.state.stages[StageName.PROBE.value].data = summary
    return {"summary": out_dir / "_summary.json"}


# ---------------------------------------------------------------------
# 2. subtitle：字幕准备（外挂 SRT 优先，内嵌次之）
# ---------------------------------------------------------------------
def run_subtitle(ctx: StageContext) -> dict:
    out_dir = ctx.stage_dir(StageName.SUBTITLE)
    found: list[dict] = []
    outputs: dict[str, Path] = {}

    for src in ctx.state.sources:
        p = Path(src.path)
        # a) 外挂字幕：同名 .srt / .ass
        sidecar = None
        for ext in (".srt", ".ass", ".ssa", ".vtt"):
            cand = p.with_suffix(ext)
            if cand.exists():
                sidecar = cand
                break

        dest = out_dir / f"{src.media_id}.srt"
        source_kind = ""
        if sidecar:
            if sidecar.suffix.lower() == ".srt":
                shutil.copy2(sidecar, dest)
            else:
                # ass/vtt 统一转 srt（M1 简化：仅 ass 用 ffmpeg 转换，vtt 直接读取后写出）
                from taggycat.media import extract_subtitle  # 局部导入避免循环

                if sidecar.suffix.lower() in (".ass", ".ssa"):
                    try:
                        _ass_to_srt(sidecar, dest)
                    except Exception as e:  # noqa: BLE001
                        logger.warning("外挂字幕转换失败 {}: {}", sidecar.name, e)
                        continue
                else:
                    sub = read_srt(sidecar)
                    from taggycat.media import write_srt

                    write_srt(sub, dest)
            source_kind = f"sidecar:{sidecar.suffix}"
        else:
            # b) 内嵌字幕流
            media = _saved_probe(ctx, src.media_id)
            if media is None:
                media = probe(p)
            if media.subtitle_streams:
                from taggycat.media import extract_subtitle

                try:
                    extract_subtitle(p, dest, stream_index=media.subtitle_streams[0].index,
                                     info=media)
                    source_kind = "embedded"
                except Exception as e:  # noqa: BLE001
                    logger.warning("内嵌字幕提取失败 {}: {}", p.name, e)
                    continue
            else:
                logger.info("  {} 无字幕（后续走 ASR 轨）", p.name)
                continue

        if dest.exists():
            try:
                sub = read_srt(dest)
                found.append({
                    "media_id": src.media_id,
                    "kind": source_kind,
                    "cues": len(sub.cues),
                    "duration": round(sub.duration, 2),
                    "path": str(dest),
                })
                outputs[src.media_id] = dest
            except Exception as e:  # noqa: BLE001
                logger.warning("字幕解析失败 {}: {}", dest.name, e)

    summary = {"with_subtitle": len(found), "total": len(ctx.state.sources), "items": found}
    (out_dir / "_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    ctx.state.stages[StageName.SUBTITLE.value].data = summary
    outputs["summary"] = out_dir / "_summary.json"
    return outputs


def _ass_to_srt(ass_path: Path, out_path: Path) -> None:
    from taggycat.media.ffmpeg import ffmpeg_bin, run_command

    cmd = [ffmpeg_bin(), "-hide_banner", "-y", "-i", str(ass_path), str(out_path)]
    run_command(cmd, timeout=120)


# ---------------------------------------------------------------------
# 3. asr：台词转录（外挂字幕优先，其次 faster-whisper）
# ---------------------------------------------------------------------
def run_asr(ctx: StageContext) -> dict:
    from taggycat.asr import Transcript, TranscriptItem, transcribe_video
    from taggycat.media import read_srt

    cfg = ctx.config
    out_dir = ctx.stage_dir(StageName.ASR)
    outputs: dict[str, Path] = {}
    summary_items: list[dict] = []

    for src in ctx.state.sources:
        dest = out_dir / f"{src.media_id}.json"
        sub_file = ctx.stage_dir(StageName.SUBTITLE) / f"{src.media_id}.srt"

        # a) 外挂/内嵌字幕优先
        if sub_file.exists():
            try:
                sub = read_srt(sub_file)
                tr = Transcript(
                    language=cfg.asr.language,
                    items=[
                        TranscriptItem(start=c.start, end=c.end, text=c.text.replace("\n", " "))
                        for c in sub.cues
                    ],
                )
                if tr.items:
                    tr.save(dest)
                    outputs[src.media_id] = dest
                    summary_items.append(
                        {"media_id": src.media_id, "source": "subtitle", "segments": len(tr.items)}
                    )
                    logger.info("  {} 字幕轨: {} 段", src.name, len(tr.items))
                    continue
            except Exception as e:  # noqa: BLE001
                logger.warning("字幕读取失败，转 ASR: {}", e)

        # b) ASR 转录（faster-whisper 本地 / ComfyUI 工作流）
        if cfg.asr.enabled and cfg.asr.engine in ("faster_whisper", "comfyui"):
            tr = transcribe_video(
                src.path, cfg.asr,
                work_dir=out_dir / src.media_id,
                root_dir=cfg.root_dir,
                media_info=_saved_probe(ctx, src.media_id),
            )
            tr.save(dest)
            outputs[src.media_id] = dest
            summary_items.append(
                {"media_id": src.media_id, "source": f"asr:{cfg.asr.engine}",
                 "segments": len(tr.items)}
            )
        else:
            logger.info("  {} ASR 已关闭且无字幕 → 台词轨为空", src.name)
            Transcript().save(dest)
            outputs[src.media_id] = dest
            summary_items.append(
                {"media_id": src.media_id, "source": "none", "segments": 0}
            )

    summary = {"items": summary_items,
               "total_segments": sum(i["segments"] for i in summary_items)}
    (out_dir / "_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    ctx.state.stages[StageName.ASR.value].data = summary
    outputs["summary"] = out_dir / "_summary.json"
    return outputs


# ---------------------------------------------------------------------
# 4. frames：场景切分 + 均匀抽帧（M1 实现，理解在 M2 接上）
# ---------------------------------------------------------------------
def run_frames(ctx: StageContext) -> dict:
    cfg = ctx.config
    out_root = ctx.stage_dir(StageName.FRAMES)
    interval = max(cfg.vlm.frame_interval, 0.5)
    max_frames = max(cfg.vlm.max_frames, 1)
    outputs: dict[str, Path] = {}
    manifest: list[dict] = []

    for src in ctx.state.sources:
        media_dir = out_root / src.media_id
        media_dir.mkdir(parents=True, exist_ok=True)
        # 复用 probe 阶段落盘信息（避免重复全参 ffprobe）
        info = _saved_probe(ctx, src.media_id)
        duration = info.duration if info is not None else 0.0

        # a/b) 场景切分 + 场景帧：单条 ffmpeg 一次遍历产出
        scene_dir = media_dir / "scene_frames"
        cuts, scene_frames = detect_scenes_with_frames(
            src.path, scene_dir,
            threshold=cfg.material.scene_threshold,
            scale_width=640, info=info,
        )
        cuts_path = media_dir / "scenes.json"
        cuts_path.write_text(
            json.dumps([c.model_dump() for c in cuts], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        # c) 均匀抽帧补足（scene_first 时仅在不足 max_frames 时补齐）
        uniform_dir = media_dir / "uniform_frames"
        use_interval = interval
        need_uniform = cfg.vlm.scene_first and len(scene_frames) < max_frames
        uniform_frames: list[Path] = []
        if need_uniform or not cfg.vlm.scene_first:
            # 按剩余预算控制间隔
            budget = max(0, max_frames - len(scene_frames)) if cfg.vlm.scene_first else max_frames
            if budget > 0 and duration > 0:
                est = int(duration / interval) + 1
                use_interval = interval if est <= budget else max(duration / budget, 0.5)
                uniform_frames = extract_frames(
                    src.path, uniform_dir, interval=use_interval,
                    max_frames=budget, scale_width=640, info=info,
                )

        # d) 帧时间戳清单（供 understand 阶段使用）
        frame_list: list[dict] = []
        for p, t in scene_frames:
            frame_list.append({"path": str(p), "t": round(t, 3), "kind": "scene"})
        # 均匀帧：文件名 frame_%06d.jpg，第 n 帧 ≈ (n-1)*use_interval
        for idx, p in enumerate(uniform_frames, start=1):
            t = (idx - 1) * (use_interval if duration else interval)
            frame_list.append({"path": str(p), "t": round(t, 3), "kind": "uniform"})
        frame_list.sort(key=lambda f: f["t"])
        frames_json = media_dir / "frames.json"
        frames_json.write_text(
            json.dumps(frame_list, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        outputs[src.media_id] = media_dir
        manifest.append({
            "media_id": src.media_id,
            "duration": duration,
            "scene_cuts": len(cuts),
            "scene_frames": len(scene_frames),
            "uniform_frames": len(uniform_frames),
            "frames_json": str(frames_json),
            "frames_dir": str(media_dir),
        })
        logger.info(
            "  {}: 切点 {} | 场景帧 {} | 均匀帧 {}",
            src.name, len(cuts), len(scene_frames), len(uniform_frames),
        )

    manifest_path = out_root / "_manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    summary = {
        "media_count": len(manifest),
        "total_scene_frames": sum(m["scene_frames"] for m in manifest),
        "total_uniform_frames": sum(m["uniform_frames"] for m in manifest),
    }
    ctx.state.stages[StageName.FRAMES.value].data = summary
    outputs["manifest"] = manifest_path
    return outputs


# ---------------------------------------------------------------------
# 5. understand：VLM 画面理解 + 台词 → 统一剧情时间线
# ---------------------------------------------------------------------
def run_understand(ctx: StageContext) -> dict:
    from taggycat.asr import Transcript
    from taggycat.llm import get_vlm
    from taggycat.models import SceneCut
    from taggycat.timeline import build_timeline

    cfg = ctx.config
    out_dir = ctx.stage_dir(StageName.UNDERSTAND)
    vlm = get_vlm(cfg)
    outputs: dict[str, Path] = {}
    summary: list[dict] = []

    # 帧级细分进度（任务表当前阶段列显示 understand x/y帧）
    frame_counts: dict[str, int] = {}
    for _src in ctx.state.sources:
        _fj = ctx.stage_dir(StageName.FRAMES) / _src.media_id / "frames.json"
        try:
            frame_counts[_src.media_id] = (
                len(json.loads(_fj.read_text(encoding="utf-8")))
                if _fj.exists() else 0
            )
        except Exception:  # noqa: BLE001
            frame_counts[_src.media_id] = 0
    total_frames = sum(frame_counts.values())
    st = ctx.state.stage(StageName.UNDERSTAND.value)
    if "progress" in (st.data or {}):        # 上次残留的细分进度先清掉
        st.data.pop("progress", None)
        ctx.store.save(ctx.state)
    done_before = 0

    def _report(done: int) -> None:
        if total_frames <= 0:
            return
        st.data = {
            **(st.data or {}),
            "progress": {
                "done": min(max(done, 0), total_frames),
                "total": total_frames,
                "label": "帧",
            },
        }
        try:
            ctx.store.save(ctx.state)
        except Exception:  # noqa: BLE001
            logger.debug("保存 understand 细分进度失败", exc_info=True)

    for src in ctx.state.sources:
        media_dir = ctx.stage_dir(StageName.FRAMES) / src.media_id
        frames_json = media_dir / "frames.json"
        scenes_json = media_dir / "scenes.json"
        transcript_file = ctx.stage_dir(StageName.ASR) / f"{src.media_id}.json"

        # 时长
        duration = 0.0
        probe_file = ctx.stage_dir(StageName.PROBE) / f"{src.media_id}.json"
        if probe_file.exists():
            duration = MediaInfo.model_validate_json(
                probe_file.read_text(encoding="utf-8")
            ).duration

        frames = json.loads(frames_json.read_text(encoding="utf-8")) if frames_json.exists() else []
        if scenes_json.exists():
            cuts = [SceneCut.model_validate(c)
                    for c in json.loads(scenes_json.read_text(encoding="utf-8"))]
        else:
            cuts = []
        tr = Transcript.load(transcript_file) if transcript_file.exists() else Transcript()

        # a) VLM 描述（可用则调用，失败/不可用则降级纯台词）
        frame_descs: list[dict] = []
        vlm_ok = False
        if vlm and frames:
            targets = [(f["t"], f["path"]) for f in frames]
            n_media = frame_counts.get(src.media_id, len(frames))

            def _cb(d: int, t: int, _base: int = done_before,
                    _cap: int = n_media) -> None:
                _report(_base + min(d, _cap or t))

            frame_descs = vlm.describe_batch(
                targets, batch_size=6,
                style_hint=cfg.output.style,
                on_progress=_cb,
            )
            _report(done_before + n_media)   # 本素材收口
            vlm_ok = bool(frame_descs)
            logger.info("  {}: VLM 描述 {}/{} 帧", src.name, len(frame_descs), len(frames))
        elif not vlm:
            logger.info("  {}: VLM 未配置，时间线仅含台词", src.name)

        # b) 融合时间线
        tl = build_timeline(
            src.media_id, duration,
            transcript=tr, frame_descs=frame_descs, cuts=cuts,
            bucket=30.0,
        )
        tl_path = out_dir / f"{src.media_id}.json"
        tl.save(tl_path)
        outputs[src.media_id] = tl_path
        summary.append({
            "media_id": src.media_id,
            "entries": len(tl.entries),
            "dialog_entries": sum(1 for e in tl.entries if e.dialog),
            "visual_entries": sum(1 for e in tl.entries if e.visual),
            "dialog_segments": len(tr.items),
            "vlm": vlm_ok,
        })
        done_before += frame_counts.get(src.media_id, 0)

    total_entries = sum(s["entries"] for s in summary)
    total_visual = sum(s["visual_entries"] for s in summary)
    coverage = (total_visual / total_entries) if total_entries else 0.0
    total = {
        "media": summary,
        "total_entries": total_entries,
        "any_vlm": any(s["vlm"] for s in summary),
        "visual_coverage": round(coverage, 3),
    }
    if total_entries and coverage < 0.5:
        logger.warning(
            "画面时间线覆盖率 {:.0%}（{}/{} 时间桶有画面描述）偏低，讲解-画面匹配会受影响；"
            "长视频建议调大 [vlm].max_frames 或调小 frame_interval 后重跑 understand",
            coverage, total_visual, total_entries,
        )
    (out_dir / "_summary.json").write_text(
        json.dumps(total, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    ctx.state.stages[StageName.UNDERSTAND.value].data = total
    outputs["summary"] = out_dir / "_summary.json"
    return outputs


# ---------------------------------------------------------------------
# 装配默认流水线（M1 阶段已实现，其余占位）
# ---------------------------------------------------------------------
def build_pipeline(config) -> Pipeline:
    from taggycat.config import AppConfig

    assert isinstance(config, AppConfig)
    p = Pipeline(config)
    p.register(StageSpec(StageName.PROBE, run_probe, "媒体探测"))
    p.register(StageSpec(StageName.SUBTITLE, run_subtitle, "字幕准备"))
    p.register(StageSpec(StageName.ASR, run_asr, "台词转录（字幕优先/ASR 兜底）"))
    p.register(StageSpec(StageName.FRAMES, run_frames, "场景切分与抽帧"))
    p.register(StageSpec(StageName.UNDERSTAND, run_understand, "VLM 画面理解 + 时间线"))
    from taggycat.pipeline.stages_m3 import register_m3
    from taggycat.pipeline.stages_m4 import register_m4

    register_m3(p)
    register_m4(p)
    return p
