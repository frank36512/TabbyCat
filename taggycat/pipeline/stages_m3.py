"""M3 阶段：剧情分析 → 分段规划 → 解说文案 → 匹配修复 → 文案校验。"""

from __future__ import annotations

import json
from pathlib import Path

from loguru import logger

from taggycat.asr import Transcript
from taggycat.models import MediaInfo, Segment, StageName
from taggycat.pipeline.orchestrator import ReviewRequired, StageContext, StageSpec
from taggycat.pipeline.segments_io import load_segments, write_segments
from taggycat.prompts import (
    ANALYZE_SYS,
    ANALYZE_USER,
    DEFAULT_SPEECH_RATE,
    PLAN_SYS,
    PLAN_USER,
    POLISH_SYS,
    POLISH_USER,
    REPAIR_SYS,
    REPAIR_USER,
    SCRIPT_SYS,
    SCRIPT_USER,
    STORYLINE_SYS,
    STORYLINE_USER,
    get_style,
    pov_line,
    style_card,
    style_edge_rate,
    style_endings,
    style_hooks,
    style_sample,
)
from taggycat.timeline import Timeline

# 中文口播基准语速（字/秒）；各风格可在基准上快慢微调（见 prompts.style_rate）
SPEECH_RATE = DEFAULT_SPEECH_RATE


def _parse_edge_rate(rate: str) -> float:
    """把 edge rate 字符串（如 "+15%"/"-10%"）换算成字/秒；非法值回退基准。"""
    s = (rate or "").strip()
    if not s:
        return SPEECH_RATE
    try:
        pct = float(s.rstrip("%"))
    except ValueError:
        return SPEECH_RATE
    return round(max(1.0, SPEECH_RATE * (1 + pct / 100)), 3)


def _effective_edge_rate(ctx: StageContext, style) -> str:
    """任务实际使用的 edge 语速：任务覆盖 > 全局非默认 > 风格语速。"""
    project = ctx.state.project
    if project.rate and project.rate.strip():
        return project.rate.strip()
    configured = (ctx.config.tts.edge.rate or "").strip()
    if configured and configured not in ("+0%", "+0", "0%"):
        return configured
    return style_edge_rate(style)


def _script_rate(ctx: StageContext, style) -> float:
    """文案字数预算所用语速。

    仅 edge 引擎可控语速：按其有效语速换算；ComfyUI 等不可控引擎沿用基准，
    避免文案字数与实际配音时长脱节（配音超长会引发画面定格）。
    """
    engine = (ctx.state.project.tts_engine or ctx.config.tts.engine or "edge").lower()
    if engine != "edge":
        return SPEECH_RATE
    return _parse_edge_rate(_effective_edge_rate(ctx, style))


# 报幕式/流水账式开场黑名单（非 LLM 质检：首段不应用这些「今天给大家讲」式套话）
_HOOK_BANNED = (
    "今天给大家", "今天我们来", "本期视频", "本视频", "接下来我们",
    "大家好，今天", "这期视频", "接下来给大家",
)


def _hook_report(segs: list[Segment]) -> str:
    """首段钩子粗检：命中报幕式套话则返回提示（不影响流程）。"""
    if not segs:
        return ""
    head = (segs[0].caption or "").strip()[:40]
    for bad in _HOOK_BANNED:
        if bad in head:
            return f"首段疑似报幕式开场「{bad}」，未化用风格钩子"
    return ""


# ---------------------------------------------------------------------
# 6. analyze：剧情分析
# ---------------------------------------------------------------------
def _normalize_analysis(raw) -> dict | None:
    """把 LLM 可能返回的数组/包装形态归一为分析对象；无法识别返回 None。

    本地模型常见两种偏差：
    - 只输出 characters 数组 `[{"name":..,"desc":..}, ...]`
    - 只输出 highlights 数组 `[{"start":..,"end":..}, ...]`
    """
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, list):
        if not raw:
            return {"characters": []}
        if not isinstance(raw[0], dict):
            return None
        head = raw[0]
        if any(k in head for k in ("start", "end", "reason")):
            return {"highlights": raw}
        if any(k in head for k in ("name", "desc")):
            return {"characters": raw}
    return None


_ANALYZE_FIX_MSG = (
    "你只返回了部分内容。顶层必须是一个 JSON 对象，"
    "包含 synopsis、characters、highlights、tone_advice 四个键，"
    "不能是数组。请重新完整输出。"
)


def run_analyze(ctx: StageContext) -> dict:
    from taggycat.llm import get_llm

    out_dir = ctx.stage_dir(StageName.ANALYZE)
    llm = get_llm(ctx.config)
    style = get_style(ctx.state.project.style)

    # 汇总所有素材的时间线
    tl_parts: list[str] = []
    total_duration = 0.0
    for src in ctx.state.sources:
        tl_file = ctx.stage_dir(StageName.UNDERSTAND) / f"{src.media_id}.json"
        probe_file = ctx.stage_dir(StageName.PROBE) / f"{src.media_id}.json"
        if probe_file.exists():
            total_duration += MediaInfo.model_validate_json(
                probe_file.read_text(encoding="utf-8")
            ).duration
        if not tl_file.exists():
            continue
        tl = Timeline.load(tl_file)
        tl_parts.append(f"### 素材：{src.name}")
        for e in tl.entries:
            s = e.summary
            if s:
                tl_parts.append(f"[{e.start:.0f}-{e.end:.0f}s] {s}")

    if not tl_parts:
        raise RuntimeError("无时间线数据，请先完成 understand 阶段")

    # 控制输入规模
    timeline_text = "\n".join(tl_parts)
    if len(timeline_text) > 24000:
        timeline_text = timeline_text[:24000] + "\n…(截断)"

    user = ANALYZE_USER.format(
        name=ctx.state.name,
        timeline=timeline_text,
        style=style.name,
        style_card=style_card(style, "analyze"),
        target=ctx.state.project.target_duration,
        duration=round(total_duration, 1),
    )
    messages = [
        {"role": "system", "content": ANALYZE_SYS},
        {"role": "user", "content": user},
    ]
    raw = llm.chat_json(messages)
    analysis = _normalize_analysis(raw)

    # 归一失败或缺 synopsis（本地模型常只回角色数组）→ 纠正一次
    if analysis is None or "synopsis" not in analysis:
        logger.warning(
            "剧情分析结果不完整（{}），要求完整 JSON 对象重试一次",
            type(raw).__name__,
        )
        asst = raw if isinstance(raw, str) else json.dumps(
            raw, ensure_ascii=False, default=str
        )
        try:
            raw2 = llm.chat_json(
                messages + [
                    {"role": "assistant", "content": asst},
                    {"role": "user", "content": _ANALYZE_FIX_MSG},
                ]
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("剧情分析纠正重试失败: {}", e)
            raw2 = None
        n2 = _normalize_analysis(raw2)
        if n2 is not None:
            # 首次结果字段优先保留（如 characters），重试补充 synopsis 等
            analysis = {**(analysis or {}), **n2}

    if analysis is None:
        raise RuntimeError(f"分析结果格式错误: {type(raw)}")

    # 校正 highlights 越界
    dur = max(total_duration, 1.0)
    for h in analysis.get("highlights", []) or []:
        try:
            h["start"] = max(0.0, min(float(h["start"]), dur))
            h["end"] = max(h["start"] + 1.0, min(float(h["end"]), dur))
        except (KeyError, TypeError, ValueError):
            continue

    out_file = out_dir / "analysis.json"
    out_file.write_text(json.dumps(analysis, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(
        "剧情分析完成: {} 角色, {} 高光",
        len(analysis.get("highlights") and analysis.get("characters") or []),
        len(analysis.get("highlights") or []),
    )
    ctx.state.stages[StageName.ANALYZE.value].data = {
        "characters": len(analysis.get("characters") or []),
        "highlights": len(analysis.get("highlights") or []),
    }
    return {"analysis": out_file}


# ---------------------------------------------------------------------
# 7. plan：分段规划（素材选取比例）
# ---------------------------------------------------------------------
def _build_candidates(ctx: StageContext) -> list[dict]:
    """从场景切点构造候选片段：累积切点直到 [min, max] 时长窗口。"""
    mc = ctx.config.material
    avg = (mc.clip_min_duration + mc.clip_max_duration) / 2
    cands: list[dict] = []

    for src in ctx.state.sources:
        scenes_file = ctx.stage_dir(StageName.FRAMES) / src.media_id / "scenes.json"
        probe_file = ctx.stage_dir(StageName.PROBE) / f"{src.media_id}.json"
        if not probe_file.exists():
            continue
        info = MediaInfo.model_validate_json(probe_file.read_text(encoding="utf-8"))
        bounds = [0.0]
        if scenes_file.exists():
            for c in json.loads(scenes_file.read_text(encoding="utf-8")):
                t = float(c.get("time_sec", 0))
                if 0 < t < info.duration:
                    bounds.append(t)
        bounds.append(info.duration)
        bounds = sorted(set(bounds))

        # 累积边界直到达到 avg 长度，形成窗口
        windows: list[tuple[float, float]] = []
        win_start = bounds[0]
        for i in range(1, len(bounds)):
            if bounds[i] - win_start >= avg:
                windows.append((win_start, bounds[i]))
                win_start = bounds[i]
        if info.duration - win_start >= mc.clip_min_duration * 0.5:
            windows.append((win_start, info.duration))

        # 超长窗口切分为 max 内的块
        final: list[tuple[float, float]] = []
        for s, e in windows:
            length = e - s
            if length <= mc.clip_max_duration:
                final.append((s, e))
            else:
                n = int(length // mc.clip_max_duration) + 1
                step = length / n
                for k in range(n):
                    final.append((s + k * step, s + (k + 1) * step))

        tl_file = ctx.stage_dir(StageName.UNDERSTAND) / f"{src.media_id}.json"
        tl = Timeline.load(tl_file) if tl_file.exists() else None
        for s, e in final:
            if e - s < mc.clip_min_duration * 0.5:
                continue
            # 聚合窗口内全部画面描述（选材依据要懂画面），无画面则退回首条台词
            visuals: list[str] = []
            dialogs: list[str] = []
            if tl:
                for entry in tl.entries:
                    if entry.end <= s or entry.start >= e:
                        continue
                    if entry.visual:
                        visuals.append(entry.visual)
                    if entry.dialog:
                        dialogs.append(entry.dialog)
            summary = "；".join(visuals)[:120] or (" ".join(dialogs)[:80] if dialogs else "")
            cands.append({
                "id": f"c{len(cands):03d}",
                "media_id": src.media_id,
                "source": src.name,
                "start": round(s, 2),
                "end": round(e, 2),
                "summary": summary,
            })
    return cands


def _sample_evenly(cands: list[dict], cap: float) -> list[dict]:
    """素材过剩时按覆盖位置均匀保留候选，使候选累计时长 ≈ cap（覆盖全片含片尾）。"""
    total = sum(c["end"] - c["start"] for c in cands)
    if not cands or total <= cap:
        return cands
    n_keep = min(len(cands), max(8, int(len(cands) * cap / total)))
    step = len(cands) / n_keep
    kept = [cands[min(int(i * step), len(cands) - 1)] for i in range(n_keep)]
    if kept[-1] is not cands[-1]:
        kept[-1] = cands[-1]
    return kept


def run_plan(ctx: StageContext) -> dict:
    from taggycat.llm import get_llm

    out_dir = ctx.stage_dir(StageName.PLAN)
    llm = get_llm(ctx.config)
    project = ctx.state.project
    style = get_style(project.style)
    mc = ctx.config.material

    analysis_file = ctx.stage_dir(StageName.ANALYZE) / "analysis.json"
    analysis = (
        json.loads(analysis_file.read_text(encoding="utf-8")) if analysis_file.exists() else {}
    )

    cands = _build_candidates(ctx)
    if not cands:
        raise RuntimeError("无候选片段（frames 阶段产物缺失？）")

    source_total = sum(c["end"] - c["start"] for c in cands)
    # 素材选取比例（新语义）：素材过剩时从源片分层采样多少比例的候选覆盖范围；
    # 仅当采样后仍能覆盖目标时长的 120% 时生效（短素材不受影响）。
    cap = source_total * project.material_ratio / 100.0
    if cap >= project.target_duration * 1.2:
        cands = _sample_evenly(cands, cap)

    # 成片按目标时长出片（提示词容差 ±20%），素材选取比例不再削减预算
    budget = float(project.target_duration)
    avg_len = max((mc.clip_min_duration + mc.clip_max_duration) / 2, 1.0)
    count = max(3, round(budget / avg_len))

    # 候选过多时按均匀采样控制提示词规模
    if len(cands) > 120:
        step = len(cands) / 120
        sampled = [cands[int(i * step)] for i in range(120)]
    else:
        sampled = cands

    cand_text = "\n".join(
        f"{c['id']} | {c['source']} | {c['start']:.1f}-{c['end']:.1f}s | {c.get('summary','')}"
        for c in sampled
    )
    user = PLAN_USER.format(
        style=style.name,
        style_card=style_card(style, "select"),
        source_total=round(sum(c["end"] - c["start"] for c in cands)),
        count=count,
        budget=round(budget),
        min_len=mc.clip_min_duration,
        max_len=mc.clip_max_duration,
        analysis=json.dumps(analysis, ensure_ascii=False)[:4000],
        candidates=cand_text,
    )
    result = llm.chat_json([
        {"role": "system", "content": PLAN_SYS},
        {"role": "user", "content": user},
    ])

    by_id = {c["id"]: c for c in sampled}
    segments: list[Segment] = []
    picked_ids: set[str] = set()
    chosen = result.get("chosen", []) if isinstance(result, dict) else []
    for item in chosen:
        cid = item.get("id") if isinstance(item, dict) else str(item)
        c = by_id.get(cid)
        if not c:
            continue
        picked_ids.add(cid)
        start, end = c["start"], c["end"]
        # 裁剪到硬限制
        if end - start > mc.clip_max_duration:
            end = start + mc.clip_max_duration
        if end - start < mc.clip_min_duration:
            end = min(start + mc.clip_min_duration, c["end"] + mc.clip_min_duration)
        segments.append(Segment(
            segment_id=f"s{len(segments) + 1:03d}",
            source_id=c["media_id"],
            start=start,
            end=end,
            reason=(item.get("reason", "") if isinstance(item, dict) else ""),
            transcript=c.get("summary", ""),
            plan_yield=bool(item.get("yield")) if isinstance(item, dict) else False,
        ))

    if not segments:
        raise RuntimeError("LLM 未选出任何片段")

    # 时长兜底：不足目标 80% 时从未选候选按时间顺序补选，补到 ≥90% 目标或候选耗尽
    target_total = float(project.target_duration)
    total = sum(s.clip_duration for s in segments)
    if total < target_total * 0.8:
        filled = 0
        for c in sampled:
            if total >= target_total * 0.9:
                break
            if c["id"] in picked_ids:
                continue
            segments.append(Segment(
                segment_id=f"s{len(segments) + 1:03d}",
                source_id=c["media_id"],
                start=c["start"],
                end=c["end"],
                reason="时长补足",
                transcript=c.get("summary", ""),
            ))
            total += c["end"] - c["start"]
            filled += 1
        if filled:
            logger.info("时长补足: 追加 {} 片段至 {:.1f}s (目标 {}s)", filled, total,
                        project.target_duration)

    out_file = write_segments(out_dir / "segments.json", segments)
    total = sum(s.clip_duration for s in segments)
    logger.info(
        "规划完成: {} 片段, 素材总长 {:.1f}s (预算 {:.0f}s, 素材比例 {}%)",
        len(segments), total, budget, project.material_ratio,
    )
    ctx.state.stages[StageName.PLAN.value].data = {
        "segments": len(segments),
        "clip_duration_total": round(total, 1),
        "budget": round(budget, 1),
    }
    return {"segments": out_file}


def _load_segments(ctx: StageContext) -> list[Segment]:
    f = ctx.stage_dir(StageName.PLAN) / "segments.json"
    if not f.exists():
        raise RuntimeError("segments.json 不存在，请先执行 plan 阶段")
    return load_segments(f)


def _save_segments(ctx: StageContext, segs: list[Segment], stage: StageName) -> Path:
    return write_segments(ctx.stage_dir(stage) / "segments.json", segs)


def _load_timeline(ctx: StageContext, media_id: str) -> Timeline | None:
    f = ctx.stage_dir(StageName.UNDERSTAND) / f"{media_id}.json"
    if f.exists():
        return Timeline.load(f)
    return None


def _segment_context(ctx: StageContext, seg: Segment) -> tuple[str, str]:
    """取片段台词与画面。"""
    tl = _load_timeline(ctx, seg.source_id)
    dialogs, visuals = [], []
    if tl:
        for e in tl.entries:
            if e.end <= seg.start or e.start >= seg.end:
                continue
            if e.dialog:
                dialogs.append(e.dialog)
            if e.visual:
                visuals.append(e.visual)
    tr_file = ctx.stage_dir(StageName.ASR) / f"{seg.source_id}.json"
    if not dialogs and tr_file.exists():
        tr = Transcript.load(tr_file)
        dialogs = [i.text for i in tr.items if i.end > seg.start and i.start < seg.end]
    return " ".join(dialogs)[:600], "；".join(visuals)[:600]


# ---------------------------------------------------------------------
# 8. script：故事节拍骨架 → 解说文案生成
# ---------------------------------------------------------------------
def _parse_script_items(arr, expected: list[str]) -> dict[str, str]:
    """归一化 LLM 文案返回：dict 包装取首个 list、id 归一化、条数一致按序回填。"""
    if isinstance(arr, dict):
        arr = next((v for v in arr.values() if isinstance(v, list)), [])
    if not isinstance(arr, list):
        return {}
    norm: dict[str, str] = {}
    for it in arr:
        if not isinstance(it, dict) or not it.get("caption"):
            continue
        raw = str(it.get("id", "")).strip()
        if raw.isdigit():                      # int/纯数字 → s001
            raw = f"s{int(raw):03d}"
        norm[raw] = str(it["caption"]).strip()
    out = {sid: norm[sid] for sid in expected if sid in norm}
    if not out and len(norm) == len(expected):  # id 全对不上但条数一致 → 按序回填
        out = dict(zip(expected, norm.values(), strict=True))
    return out


def _chars_text(analysis: dict, limit: int = 8) -> str:
    """角色表紧凑文本（供 storyline/script/polish 注入）。"""
    return "；".join(
        f"{c.get('name', '')}（{c.get('desc', '')}）"
        for c in (analysis.get("characters") or [])[:limit]
        if isinstance(c, dict) and c.get("name")
    ) or "（无）"


def _build_storyline(
    ctx: StageContext, llm, style: object, segs: list[Segment],
    ctx_map: dict[str, tuple[str, str]], synopsis: str, analysis: dict,
) -> tuple[str, bool, str]:
    """一次 LLM 调用搭出全集故事骨架，返回 (节拍文本, 是否成功, 主线文本)。

    主线文本形如「主角：阿哲｜他要在天亮前找出内鬼，否则全家陪葬」，
    会注入每个文案批次，保证分批写作时全集故事脊柱不丢。
    失败/返回异常时返回 ("", False, "")，文案阶段降级为无骨架模式，不阻断流水线。
    """
    lines: list[str] = []
    for s in segs:
        d, v = ctx_map[s.segment_id]
        lines.append(
            f"- id={s.segment_id} 时长={s.clip_duration:.1f}s "
            f"理由={s.reason or '（无）'} | {(v or d or '（无）')[:160]}"
        )
    user = STORYLINE_USER.format(
        style=style.name, style_card=style_card(style, "story"),
        synopsis=synopsis or "（无）", characters=_chars_text(analysis),
        segments="\n".join(lines), n=len(segs),
    )
    try:
        resp = llm.chat_json([
            {"role": "system", "content": STORYLINE_SYS},
            {"role": "user", "content": user},
        ])
    except Exception as e:  # noqa: BLE001
        logger.warning("故事节拍生成失败，文案将无骨架参考: {}", e)
        return "", False, ""

    if not isinstance(resp, dict):
        return "", False, ""
    arc = str(resp.get("arc") or "").strip()
    protagonist = str(resp.get("protagonist") or "").strip()
    arc_text = ""
    if arc:
        arc_text = f"{arc}（主角：{protagonist}）" if protagonist else arc

    beats = resp.get("beats")
    if not isinstance(beats, list) or not beats:
        return "", False, arc_text
    order = [s.segment_id for s in segs]
    by_id: dict[str, str] = {}
    for b in beats:
        if not isinstance(b, dict):
            continue
        raw = str(b.get("id", "")).strip()
        if raw.isdigit():
            raw = f"s{int(raw):03d}"
        role = str(b.get("role", "")).strip()
        point = str(b.get("point", "")).strip()
        if raw in order and point:
            by_id[raw] = f"{role}｜{point}" if role else point
    if not by_id:
        return "", False, arc_text
    out_dir = ctx.stage_dir(StageName.SCRIPT)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "storyline.json").write_text(
        json.dumps({"arc": arc, "protagonist": protagonist, "beats": beats},
                   ensure_ascii=False, indent=2), encoding="utf-8"
    )
    text = "\n".join(
        f"{sid}: {by_id.get(sid, '（按画面自然推进）')}" for sid in order
    )
    logger.info(
        "故事节拍骨架完成: {}/{} 拍有明确叙事功能, 主线「{}」",
        len(by_id), len(segs), arc_text[:40] or "（未提取）",
    )
    return text, True, arc_text


def run_script(ctx: StageContext) -> dict:
    from taggycat.llm import get_llm

    llm = get_llm(ctx.config)
    style = get_style(ctx.state.project.style)
    rate = _script_rate(ctx, style)
    segs = _load_segments(ctx)

    analysis_file = ctx.stage_dir(StageName.ANALYZE) / "analysis.json"
    analysis: dict = {}
    synopsis = ""
    if analysis_file.exists():
        analysis = json.loads(analysis_file.read_text(encoding="utf-8"))
        synopsis = analysis.get("synopsis", "")

    # 逐片段上下文（台词 + 画面）
    items_text_parts: list[str] = []
    ctx_map: dict[str, tuple[str, str]] = {}
    for s in segs:
        d, v = _segment_context(ctx, s)
        ctx_map[s.segment_id] = (d, v)
        dur = s.clip_duration
        target = int(round(dur * rate))
        items_text_parts.append(
            f"- id={s.segment_id} 时长={dur:.1f}s 目标{target}字"
            f" | 台词: {d or '(无)'} | 画面: {v or '(无)'}"
        )
    part_of = {s.segment_id: items_text_parts[i] for i, s in enumerate(segs)}

    # 第一步：全集故事骨架（主线 arc + 逐拍节拍），写文案前先定故事
    beats_text, storyline_ok, arc_text = _build_storyline(
        ctx, llm, style, segs, ctx_map, synopsis, analysis
    )
    chars = _chars_text(analysis)

    captions: dict[str, str] = {}
    batch_size = 8

    def _gen(targets: list) -> dict[str, str]:
        got: dict[str, str] = {}
        for i in range(0, len(targets), batch_size):
            batch = targets[i:i + batch_size]
            block = "\n".join(part_of[s.segment_id] for s in batch)
            # 上文：本批第一段的前一条已写文案全文（保持段间衔接与伏笔承接）
            first_idx = segs.index(batch[0])
            prev_txt = ""
            for k in range(first_idx - 1, -1, -1):
                sid = segs[k].segment_id
                if sid in captions:
                    prev_txt = captions[sid]
                    break
            user = SCRIPT_USER.format(
                style=style.name,
                style_card=style_card(style, "script"),
                sample=style_sample(style),
                hooks=style_hooks(style) or "（无模板：按风格卡的开场钩子自行设计）",
                endings=style_endings(style) or "（无模板：按风格卡自行收尾）",
                pov_line=pov_line(
                    style, ctx.state.project.pov, ctx.state.project.pov_character
                ),
                synopsis=synopsis or "（无）",
                characters=chars,
                arc=arc_text or "（无主线：请自行保证全集围绕同一冲突推进）",
                beats=beats_text or "（无骨架：请自行保证起承转合）",
                prev=prev_txt or "（无）",
                segments=block, n=len(batch), rate=rate,
            )
            arr = llm.chat_json([
                {"role": "system", "content": SCRIPT_SYS.format(rate=rate)},
                {"role": "user", "content": user},
            ])
            got.update(_parse_script_items(arr, [s.segment_id for s in batch]))
        return got

    captions.update(_gen(segs))
    retried = 0
    missing = [s for s in segs if s.segment_id not in captions]
    if missing:
        logger.warning("缺少文案片段 {}，重试一次", [s.segment_id for s in missing])
        retried = len(missing)
        captions.update(_gen(missing))
        missing = [s for s in segs if s.segment_id not in captions]
    if missing:
        logger.warning("缺少文案片段: {}，将按台词拼接占位", [s.segment_id for s in missing])
        for s in missing:
            d, _ = ctx_map[s.segment_id]
            captions[s.segment_id] = d[: int(rate * 10)] or "……"

    for s in segs:
        s.caption = captions.get(s.segment_id, "")
        # 未提供 transcript 时回填
        if not s.transcript:
            s.transcript = ctx_map[s.segment_id][0]

    # 第三步：全集通读润色（衔接/称谓统一/去重复），失败静默降级
    polished = _polish_script(llm, style, segs, arc_text, chars)

    out_file = _save_segments(ctx, segs, StageName.SCRIPT)
    total_chars = sum(len(s.caption) for s in segs)
    logger.info("文案生成完成: {} 段, 共 {} 字, 润色 {} 段", len(segs), total_chars, polished)
    ctx.state.stages[StageName.SCRIPT.value].data = {
        "segments": len(segs),
        "total_chars": total_chars,
        "retried": retried,
        "storyline": storyline_ok,
        "polished": polished,
        "missing_still": [s.segment_id for s in missing],
        "missing_repaired": len(missing),
    }
    return {"segments": out_file}


def _polish_script(llm, style, segs: list[Segment], arc: str, chars: str) -> int:
    """全集通读润色：修衔接/称谓不一致/相邻重复。返回修改段数。

    防护：修改后字数变化超过 ±15% 的条目直接拒绝（防止破坏时长匹配）；
    任何异常静默返回 0，不阻断流水线。
    """
    if len(segs) < 4 or not all(s.caption for s in segs):
        return 0
    body = "\n".join(
        f"{s.segment_id}（{len(s.caption)}字）：{s.caption}" for s in segs
    )
    try:
        resp = llm.chat_json([
            {"role": "system", "content": POLISH_SYS},
            {"role": "user", "content": POLISH_USER.format(
                style=style.name, arc=arc or "（无）", characters=chars,
                captions=body,
            )},
        ])
    except Exception as e:  # noqa: BLE001
        logger.warning("全集润色调用失败，跳过: {}", e)
        return 0

    fixes = _parse_script_items(resp, [s.segment_id for s in segs])
    if not fixes:
        return 0
    applied = 0
    for s in segs:
        new = fixes.get(s.segment_id)
        if not new:
            continue
        old_len = max(len(s.caption), 1)
        if abs(len(new) - old_len) > max(4, int(old_len * 0.15)):
            logger.warning(
                "润色改写 {} 字数变化过大（{}→{}），拒绝该条",
                s.segment_id, old_len, len(new),
            )
            continue
        s.caption = new
        applied += 1
    if applied:
        logger.info("全集润色: 修改 {} 段（衔接/称谓/重复）", applied)
    return applied


# ---------------------------------------------------------------------
# 9. match：字数校验 + LLM 自动修复
# ---------------------------------------------------------------------
def _char_bounds(dur: float, rate: float = SPEECH_RATE) -> tuple[int, int, int]:
    target = int(round(dur * rate))
    return target, int(target * 0.8), int(target * 1.2)


def run_match(ctx: StageContext) -> dict:
    from taggycat.llm import get_llm

    llm = get_llm(ctx.config)
    style = get_style(ctx.state.project.style)
    rate = _script_rate(ctx, style)
    segs = _load_segments(ctx)
    # script 阶段产物优先
    script_file = ctx.stage_dir(StageName.SCRIPT) / "segments.json"
    if script_file.exists():
        segs = load_segments(script_file)

    repaired = 0
    still_bad: list[str] = []
    for s in segs:
        target, lo, hi = _char_bounds(s.clip_duration, rate)
        n = len(s.caption)
        if lo <= n <= hi:
            continue
        # 字数不合格 → LLM 修复
        try:
            result = llm.chat_json([
                {"role": "system", "content": REPAIR_SYS},
                {"role": "user", "content": REPAIR_USER.format(
                    style=style.name, style_card=style_card(style, "repair"),
                    cur=n, caption=s.caption or s.transcript,
                    target=target, lo=lo, hi=hi, dur=s.clip_duration,
                    transcript=s.transcript or "(无)",
                )},
            ])
            cap = result.get("caption") if isinstance(result, dict) else None
            if cap:
                s.caption = str(cap).strip()
                repaired += 1
        except Exception as e:  # noqa: BLE001
            logger.warning("片段 {} 修复失败: {}", s.segment_id, e)
        if not (lo <= len(s.caption) <= hi):
            still_bad.append(s.segment_id)

    out_file = _save_segments(ctx, segs, StageName.MATCH)

    # 钩子质检（非 LLM）：首段是否为报幕式开场，仅提示不阻断
    hook_issue = _hook_report(segs)
    if hook_issue:
        logger.warning("开场钩子质检：{}（风格：{}）", hook_issue, style.name)

    logger.info(
        "文案匹配完成: 修复 {} 段，{} 段字数仍不合格 {}",
        repaired, len(still_bad), still_bad or "",
    )
    ctx.state.stages[StageName.MATCH.value].data = {
        "repaired": repaired,
        "still_bad": still_bad,
        "ok": len(segs) - len(still_bad),
        "rate": rate,
        "hook_issue": hook_issue,
    }
    return {"segments": out_file}


# ---------------------------------------------------------------------
# 10. review：文案校验（开关两模式）
# ---------------------------------------------------------------------
def run_review(ctx: StageContext) -> dict:
    out_dir = ctx.stage_dir(StageName.REVIEW)
    approved_file = out_dir / "approved.json"
    validate = ctx.state.project.validate_script
    segs_file = ctx.stage_dir(StageName.MATCH) / "segments.json"
    if not segs_file.exists():
        segs_file = ctx.stage_dir(StageName.SCRIPT) / "segments.json"
    segs = load_segments(segs_file)

    still_bad = ctx.state.stages[StageName.MATCH.value].data.get("still_bad", [])

    # 模式二（关）：仅字数不合格时自动审核（LLM 校验一次）
    if not validate:
        if still_bad:
            logger.warning("校验文案关闭，但 {} 段字数不合格 → 自动 LLM 审核", len(still_bad))
            return _auto_validate(ctx, segs, out_dir)
        logger.info("校验文案关闭且字数全部合格 → 直接通过")
        _write_approval(out_dir, segs, auto=True)
        return {"approved": approved_file}

    # 模式一（开）：人工审核
    if approved_file.exists():
        logger.info("检测到人工审核通过标记，继续")
        return {"approved": approved_file}

    # 生成待审核清单
    pending = out_dir / "pending.json"
    write_segments(pending, segs)
    summary_file = out_dir / "PENDING.txt"
    summary_file.write_text(
        "待人工审核文案：\n"
        f"  任务: {ctx.state.task_id}\n"
        f"  片段数: {len(segs)}，字数不合格: {len(still_bad)}\n"
        "审阅/修改 pending.json 后执行：\n"
        f"  taggycat task approve {ctx.state.task_id} [--file <segments.json>]\n",
        encoding="utf-8",
    )
    raise ReviewRequired(
        f"需要人工审核文案：{pending}\n"
        f"完成后执行 `taggycat task approve {ctx.state.task_id}` 继续"
    )


def _write_approval(out_dir: Path, segs: list[Segment], auto: bool) -> None:
    (out_dir / "approved.json").write_text(
        json.dumps({"auto": auto, "segments": len(segs)}, ensure_ascii=False),
        encoding="utf-8",
    )


def _auto_validate(ctx: StageContext, segs: list[Segment], out_dir: Path) -> dict:
    """关闭人工审核但字数不合格时的 LLM 自动审核。"""
    from taggycat.llm import get_llm
    from taggycat.prompts import VALIDATE_SYS, VALIDATE_USER

    llm = get_llm(ctx.config)
    style = get_style(ctx.state.project.style)
    rate = _script_rate(ctx, style)
    issues: list[str] = []
    fixed = 0
    for s in segs:
        target, lo, hi = _char_bounds(s.clip_duration, rate)
        if lo <= len(s.caption) <= hi:
            continue
        d, v = _segment_context(ctx, s)
        try:
            r = llm.chat_json([
                {"role": "system", "content": VALIDATE_SYS},
                {"role": "user", "content": VALIDATE_USER.format(
                    dur=s.clip_duration, target=target, lo=lo, hi=hi,
                    cur=len(s.caption), caption=s.caption,
                    transcript=s.transcript or d, visual=v,
                )},
            ])
            if isinstance(r, dict) and not r.get("pass", True):
                issues.append(f"{s.segment_id}: {'; '.join(r.get('issues') or [])}")
                sugg = r.get("suggestion") or ""
                if sugg and len(issues) <= 3:
                    s.caption = sugg
                    fixed += 1
        except Exception as e:  # noqa: BLE001
            logger.warning("自动审核失败: {}", e)

    if fixed:
        _save_segments(ctx, segs, StageName.REVIEW)
    _write_approval(out_dir, segs, auto=True)
    logger.info("自动审核完成: {} 条问题, 修正 {} 条", len(issues), fixed)
    (out_dir / "report.json").write_text(
        json.dumps({"issues": issues, "fixed": fixed}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return {"approved": out_dir / "approved.json"}


def register_m3(p) -> None:
    p.register(StageSpec(StageName.ANALYZE, run_analyze, "剧情分析"))
    p.register(StageSpec(StageName.PLAN, run_plan, "分段规划（素材选取比例）"))
    p.register(StageSpec(StageName.SCRIPT, run_script, "解说文案生成"))
    p.register(StageSpec(StageName.MATCH, run_match, "字数校验与修复"))
    p.register(StageSpec(StageName.REVIEW, run_review, "文案校验（人工/自动）"))
