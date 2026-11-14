"""虎猫解说 (taggycat) 命令行入口。"""

from __future__ import annotations

import argparse
import contextlib
import json
import shutil
import sys
from pathlib import Path

from loguru import logger


def _setup_logging(verbose: bool = False) -> None:
    logger.remove()
    level = "DEBUG" if verbose else "INFO"
    fmt = "<green>{time:HH:mm:ss}</green> | <level>{level:<7}</level> | {message}"
    # windowed exe（console=False）下 sys.stderr 为 None，loguru 无法写 None sink：
    # 控制台可用时打控制台；否则落盘到 exe 同目录 / CWD 的 taggycat.log，
    # 保证 GUI 启动失败时仍有日志可查。
    sink = sys.stderr if sys.stderr is not None else getattr(sys, "__stderr__", None)
    if sink is None:
        if getattr(sys, "frozen", False):
            base = Path(sys.executable).resolve().parent
        else:
            base = Path.cwd()
        logger.add(base / "taggycat.log", level=level, format=fmt, encoding="utf-8")
    else:
        logger.add(sink, level=level, format=fmt, colorize=True)


def _load_config(args):
    from taggycat.config import load_config

    return load_config(getattr(args, "config", None))


# ---------------------------------------------------------------------
# 子命令
# ---------------------------------------------------------------------
def cmd_init(args) -> int:
    """生成 config.toml。"""
    root = Path.cwd()
    dst = root / "config.toml"
    if dst.exists() and not args.force:
        print(f"已存在: {dst}（--force 覆盖）")
        return 0
    # 仓库根 > 包内（wheel 安装） > CWD
    candidates = [
        Path(__file__).resolve().parent.parent / "config.example.toml",
        Path(__file__).resolve().parent / "config.example.toml",
        root / "config.example.toml",
    ]
    src = next((p for p in candidates if p.exists()), None)
    if src is None:
        print("未找到配置模板 config.example.toml", file=sys.stderr)
        return 1
    shutil.copy2(src, dst)
    print(f"已生成: {dst}")
    print("请编辑 config.toml 填入 LLM/VLM 的 Base URL 与 API Key。")
    return 0


def cmd_probe(args) -> int:
    from taggycat.media import probe

    results = []
    for f in args.files:
        info = probe(f)
        results.append(info.model_dump(mode="json"))
        print(
            f"{Path(f).name}: {info.duration:.1f}s | {info.width}x{info.height} "
            f"@ {info.fps:.2f}fps | audio={info.has_audio} | "
            f"subtitle_streams={len(info.subtitle_streams)} | {info.size_bytes/1024/1024:.1f}MB"
        )
    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    return 0


def cmd_frames(args) -> int:
    from taggycat.media import detect_scenes, extract_frames, extract_frames_by_scene

    src = Path(args.file)
    out = Path(args.out or (Path.cwd() / "frames_dump" / src.stem))
    if args.scene_only:
        frames = extract_frames_by_scene(src, out, threshold=args.threshold)
        print(f"场景帧 {len(frames)} 个 -> {out}")
        for p, t in frames[:10]:
            print(f"  {t:8.2f}s  {p.name}")
    elif args.detect_only:
        cuts = detect_scenes(src, threshold=args.threshold)
        print(f"场景切点 {len(cuts)} 个:")
        for c in cuts[:50]:
            print(f"  {c.time_sec:8.2f}s  score={c.score:.3f}")
    else:
        frames = extract_frames(src, out, interval=args.interval, scale_width=640)
        print(f"均匀抽帧 {len(frames)} 帧 -> {out}")
    return 0


def cmd_stages(args) -> int:
    config = _load_config(args)
    from taggycat.pipeline import build_pipeline

    p = build_pipeline(config)
    print(f"{'#':<3} {'阶段':<14} {'状态':<12} 说明")
    print("-" * 72)
    for i, spec in enumerate(p.stage_specs, 1):
        impl = "已实现" if spec.func else "待实现"
        print(f"{i:<3} {spec.name.value:<14} {impl:<12} {spec.description}")
    return 0


def cmd_task_create(args) -> int:
    config = _load_config(args)
    from taggycat.models import ProjectConfig
    from taggycat.store import TaskStore

    project = ProjectConfig(
        language=args.language,
        style=args.style,
        voice=args.voice,
        rate=args.rate,
        pov=args.pov,
        pov_character=args.pov_character,
        tts_engine=args.engine or "",
        tts_cache=not args.no_tts_cache,
        bgm=args.bgm or "",
        material_ratio=args.material_ratio,
        narration_ratio=args.narration_ratio,
        target_duration=args.target_duration,
        validate_script=not args.no_validate_script,
        output_dir=str(config.output_dir),
    )
    store = TaskStore(config)
    state = store.create(args.files, name=args.name, project=project)
    print(f"任务已创建: {state.task_id}")
    print(f"  素材: {len(state.sources)} 个, 合计 {sum(s.size_mb for s in state.sources):.1f} MB")
    print(f"  风格: {project.style} | 音色: {project.voice or '(默认)'} | "
          f"引擎: {project.tts_engine or '全局默认'} | "
          f"素材比例: {project.material_ratio}% | 解说比例: {project.narration_ratio}%")
    print(f"  下一步: taggycat task run {state.task_id}")
    return 0


def cmd_task_list(args) -> int:
    config = _load_config(args)
    from taggycat.store import TaskStore

    store = TaskStore(config)
    tasks = store.list()
    if getattr(args, "all", False) is False:
        tasks = [t for t in tasks if not t.archived]
    if getattr(args, "json", False):
        print(json.dumps(
            [t.model_dump(mode="json") for t in tasks],
            ensure_ascii=False, indent=2,
        ))
        return 0
    if not tasks:
        print("暂无任务" if getattr(args, "all", False) else "暂无任务（归档任务用 --all 查看）")
        return 0
    print(f"{'任务 ID':<48} {'状态':<12} {'素材':<6} 创建时间  名称")
    print("-" * 110)
    for t in tasks:
        done = sum(1 for s in t.stages.values() if s.status.value == "done")
        status = "完成" if t.finished else f"{done}/{len(t.stages)}"
        if t.archived:
            status = f"归档/{status}"
        print(f"{t.task_id:<48} {status:<12} {len(t.sources):<6} "
              f"{t.created_at}  {t.name}")
    return 0


def cmd_task_rename(args) -> int:
    config = _load_config(args)
    from taggycat.store import TaskStore

    store = TaskStore(config)
    tid = store.resolve_task_id(args.task_id)
    state = store.rename(tid, args.name)
    print(f"已重命名: {state.task_id} -> {state.name}")
    return 0


def cmd_task_delete(args) -> int:
    config = _load_config(args)
    from taggycat.store import TaskStore

    store = TaskStore(config)
    tid = store.resolve_task_id(args.task_id)
    state = store.load(tid)
    running = any(s.status.value == "running" for s in state.stages.values())
    if running:
        print("错误: 任务正在运行，请先停止", file=sys.stderr)
        return 1
    if not args.yes:
        ans = input(f"确认删除任务 {tid}（含全部产物，不可恢复）？[y/N] ").strip().lower()
        if ans not in ("y", "yes"):
            print("已取消")
            return 0
    store.delete(tid)
    print(f"已删除: {tid}")
    return 0


def cmd_task_archive(args) -> int:
    config = _load_config(args)
    from taggycat.store import TaskStore

    store = TaskStore(config)
    tid = store.resolve_task_id(args.task_id)
    state = store.load(tid)
    running = any(s.status.value == "running" for s in state.stages.values())
    if running:
        print("错误: 任务正在运行，无法归档", file=sys.stderr)
        return 1
    state = store.set_archived(tid, not args.undo)
    print(f"{'已取消归档' if args.undo else '已归档'}: {state.task_id} ({state.name})")
    return 0


def cmd_task_show(args) -> int:
    config = _load_config(args)
    from taggycat.store import TaskStore

    store = TaskStore(config)
    tid = store.resolve_task_id(args.task_id)
    state = store.load(tid)
    print(f"任务: {state.task_id}  ({state.name})")
    print(f"创建: {state.created_at}   更新: {state.updated_at}")
    p = state.project
    print(f"配置: 风格={p.style} 语言={p.language} 音色={p.voice or '(默认)'} "
          f"素材比例={p.material_ratio}% 解说比例={p.narration_ratio}% "
          f"目标时长={p.target_duration}s 校验文案={'开' if p.validate_script else '关'}")
    print("素材:")
    for s in state.sources:
        print(f"  - {s.name}  ({s.size_mb} MB)")
    print("阶段:")
    for key, st in state.stages.items():
        line = f"  {key:<14} {st.status.value:<10}"
        if st.error:
            line += f" err={st.error[:60]}"
        print(line)
    return 0


def cmd_doctor(args) -> int:
    """环境与配置自检。"""
    config = _load_config(args)
    from taggycat.config import find_config
    from taggycat.doctor import FAIL, format_report, run_checks

    checks = run_checks(config, config_path=find_config(getattr(args, "config", None)))
    print(format_report(checks))
    return 1 if any(c.level == FAIL for c in checks) else 0


def cmd_ui(args) -> int:
    """启动 PyQt6 桌面界面。"""
    config = _load_config(args)
    try:
        from taggycat.ui.main_window import run_app
    except ImportError as e:
        print(
            "未安装 UI 依赖，请执行: uv sync --all-extras\n"
            f"（{e}）",
            file=sys.stderr,
        )
        return 1
    return run_app(config)


def cmd_task_approve(args) -> int:
    """人工审核通过：写 approved 标记，可选用修改后的 segments 覆盖。"""
    import json as _json

    from taggycat.models import StageStatus
    from taggycat.pipeline.segments_io import load_segments, write_segments
    from taggycat.store import TaskStore

    config = _load_config(args)
    store = TaskStore(config)
    tid = store.resolve_task_id(args.task_id)
    state = store.load(tid)
    st = state.stage("review")
    if st.status.value not in ("review", "pending"):
        print(f"review 阶段状态为 {st.status.value}，无需审核")
        return 0

    review_dir = store.stage_dir(tid, "review")
    # 可选：应用人工修改后的 segments
    if args.file:
        src = Path(args.file)
        segs = load_segments(src)
        for stage in ("script", "match"):
            write_segments(store.stage_dir(tid, stage) / "segments.json", segs)
        print(f"已应用修改文案: {src} ({len(segs)} 段)")

    (review_dir / "approved.json").write_text(
        _json.dumps({"auto": False, "approved_at": st.finished_at or ""}, ensure_ascii=False),
        encoding="utf-8",
    )
    st.status = StageStatus.DONE
    st.error = None
    store.save(state)
    print(f"审核已通过: {tid}")
    print(f"继续执行: taggycat task run {tid}")
    return 0


def cmd_task_run(args) -> int:
    config = _load_config(args)
    from taggycat.pipeline import StageError, build_pipeline

    p = build_pipeline(config)
    tid = p.store.resolve_task_id(args.task_id)
    try:
        state = p.run(
            tid,
            only_stage=args.stage,
            force=args.force,
            stop_on_unimplemented=not args.all,
            until=args.until,
        )
    except StageError as e:
        print(f"失败: {e}", file=sys.stderr)
        return 2

    done = [k for k, s in state.stages.items() if s.status.value == "done"]
    failed = [k for k, s in state.stages.items() if s.status.value == "failed"]
    review = [k for k, s in state.stages.items() if s.status.value == "review"]
    print(f"完成阶段: {', '.join(done) if done else '(无)'}")
    if failed:
        print(f"失败阶段: {', '.join(failed)}", file=sys.stderr)
        return 2
    if review:
        print(f"⏸ 等待人工审核: {', '.join(review)}")
        print(f"   审核后执行: taggycat task run {tid}")
        return 3
    cur = state.current_stage
    if state.finished:
        exp = state.stages.get("export")
        if exp and exp.data.get("output"):
            print(
                f"成片: {exp.data['output']}"
                f"（{exp.data.get('duration')}s, {exp.data.get('size_mb')} MB）"
            )
    elif cur and state.stages.get(cur) and state.stages[cur].status.value != "done":
        print(f"停在: {cur}")
    print(f"工作区: {p.store.task_dir(tid)}")
    return 0


# ---------------------------------------------------------------------
# argparse
# ---------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="taggycat",
        description="虎猫解说 (taggycat) - AI 影视解说 + 自动化剪辑",
    )
    ap.add_argument("-c", "--config", help="配置文件路径", default=None)
    ap.add_argument("-v", "--verbose", action="store_true", help="调试日志")
    sub = ap.add_subparsers(dest="command", required=True)

    # init
    sub.add_parser("init", help="生成 config.toml").add_argument(
        "--force", action="store_true", help="覆盖已有配置"
    )

    # probe
    sp = sub.add_parser("probe", help="媒体信息探测")
    sp.add_argument("files", nargs="+", help="视频文件")
    sp.add_argument("--json", action="store_true", help="输出 JSON")

    # frames
    fp = sub.add_parser("frames", help="抽帧 / 场景切分")
    fp.add_argument("file", help="视频文件")
    fp.add_argument("--out", help="输出目录")
    fp.add_argument("--interval", type=float, default=10.0, help="均匀抽帧间隔（秒）")
    fp.add_argument("--threshold", type=float, default=0.3, help="场景切分阈值")
    fp.add_argument("--scene-only", action="store_true", help="仅场景抽帧")
    fp.add_argument("--detect-only", action="store_true", help="仅场景检测（不抽帧）")

    # stages
    sub.add_parser("stages", help="列出流水线阶段")

    # doctor
    sub.add_parser("doctor", help="环境与配置自检")

    # task
    tp = sub.add_parser("task", help="任务管理")
    tsub_p = tp.add_subparsers(dest="subcmd", required=True)

    tc = tsub_p.add_parser("create", help="创建任务")
    tc.add_argument("files", nargs="+", help="素材视频（可多个）")
    tc.add_argument("--name", default="", help="任务名称")
    tc.add_argument("--style", default="电影解说", help="解说风格（见 STYLE_PRESETS）")
    tc.add_argument("--language", default="中文", help="输出语言")
    tc.add_argument("--voice", default="", help="配音音色")
    tc.add_argument("--rate", default="", help="语速覆盖（如 +10%%；默认按所选风格语速）")
    tc.add_argument("--pov", choices=["", "first", "third"], default="",
                    help="叙述视角：first=第一人称沉浸式，third=第三人称（默认跟随风格）")
    tc.add_argument("--pov-character", default="", help="第一人称代入的角色名（默认主角）")
    tc.add_argument("--engine", choices=["edge", "comfyui", "comfyui_clone"],
                    default="", help="TTS 引擎（默认用全局配置）")
    tc.add_argument("--no-tts-cache", action="store_true", help="忽略配音缓存强制重合成")
    tc.add_argument("--bgm", default="", help="背景音乐")
    tc.add_argument("--material-ratio", type=int, default=30,
                    help="素材选取比例 %%（素材过剩时候选采样覆盖比例）")
    tc.add_argument("--narration-ratio", type=int, default=50, help="解说比例 %%")
    tc.add_argument("--target-duration", type=int, default=300, help="目标成片时长（秒）")
    tc.add_argument("--no-validate-script", action="store_true", help="关闭配音前文案审核")

    tl = tsub_p.add_parser("list", help="任务列表")
    tl.add_argument("--json", action="store_true")
    tl.add_argument("--all", action="store_true", help="包含已归档任务")

    trn = tsub_p.add_parser("rename", help="重命名任务")
    trn.add_argument("task_id")
    trn.add_argument("name", help="新名称")

    td = tsub_p.add_parser("delete", help="删除任务（含产物，不可恢复）")
    td.add_argument("task_id")
    td.add_argument("--yes", "-y", action="store_true", help="跳过确认")

    tar = tsub_p.add_parser("archive", help="归档任务（默认列表隐藏）")
    tar.add_argument("task_id")
    tar.add_argument("--undo", action="store_true", help="取消归档")

    ts = tsub_p.add_parser("show", help="任务详情")
    ts.add_argument("task_id")

    tr = tsub_p.add_parser("run", help="执行任务流水线")
    tr.add_argument("task_id")
    tr.add_argument("--stage", help="只跑指定阶段")
    tr.add_argument("--force", action="store_true", help="重跑已完成阶段")
    tr.add_argument("--all", action="store_true", help="不因未实现阶段而停止")
    tr.add_argument("--until", help="执行到该阶段（含）后停止，如 understand/review")

    ta = tsub_p.add_parser("approve", help="人工审核文案通过")
    ta.add_argument("task_id")
    ta.add_argument("--file", help="修改后的 segments.json（覆盖 script/match 产物）")

    # ui
    sub.add_parser("ui", help="启动桌面界面（PyQt6）")

    return ap


def main(argv: list[str] | None = None) -> int:
    # Windows 控制台强制 UTF-8（避免中文乱码）
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(Exception):
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]

    # 单文件 exe 双击启动时没有命令行参数，默认进入桌面界面
    if argv is None and getattr(sys, "frozen", False) and not sys.argv[1:]:
        argv = ["ui"]

    ap = build_parser()
    args = ap.parse_args(argv)
    _setup_logging(args.verbose)

    try:
        if args.command == "init":
            return cmd_init(args)
        if args.command == "probe":
            return cmd_probe(args)
        if args.command == "frames":
            return cmd_frames(args)
        if args.command == "stages":
            return cmd_stages(args)
        if args.command == "doctor":
            return cmd_doctor(args)
        if args.command == "task":
            return {
                "create": cmd_task_create,
                "list": cmd_task_list,
                "show": cmd_task_show,
                "run": cmd_task_run,
                "approve": cmd_task_approve,
                "rename": cmd_task_rename,
                "delete": cmd_task_delete,
                "archive": cmd_task_archive,
            }[args.subcmd](args)
        if args.command == "ui":
            return cmd_ui(args)
    except FileNotFoundError as e:
        print(f"错误: {e}", file=sys.stderr)
        return 1
    except Exception as e:  # noqa: BLE001
        logger.exception("执行失败: {}", e)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
