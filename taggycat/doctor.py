"""环境与配置自检：taggycat doctor。"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from taggycat.config import AppConfig

OK = "ok"
WARN = "warn"
FAIL = "fail"


@dataclass
class Check:
    level: str      # ok | warn | fail
    name: str
    message: str


def _has_module(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def _ffmpeg_check() -> Check:
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        return Check(FAIL, "ffmpeg", "未找到 ffmpeg/ffprobe，请安装并加入 PATH")
    try:
        out = subprocess.run(
            ["ffmpeg", "-version"], capture_output=True, text=True, timeout=15
        ).stdout.splitlines()[0]
    except Exception as e:  # noqa: BLE001
        return Check(FAIL, "ffmpeg", f"ffmpeg 无法执行: {e}")
    return Check(OK, "ffmpeg", out[:80])


def _llm_check(cfg: AppConfig) -> Check:
    primary = cfg.llm
    if primary.api_key and "REPLACE" not in primary.api_key:
        return Check(OK, "llm", f"云端 LLM: {primary.model or '?'} @ {primary.base_url}")
    if cfg.llm_local.base_url:
        return Check(
            WARN, "llm",
            f"云端 LLM 未配置 Key，将回退本地: {cfg.llm_local.model or '?'} "
            f"@ {cfg.llm_local.base_url}",
        )
    return Check(
        FAIL, "llm",
        "LLM 未配置：请在 config.toml [llm] 填 base_url/api_key/model（或配置 [llm_local]）",
    )


def _vlm_check(cfg: AppConfig) -> Check:
    if not cfg.vlm.enabled:
        return Check(OK, "vlm", "已禁用（仅台词轨理解）")
    provider = (cfg.vlm.provider or "openai_compatible").lower()
    if provider in ("comfyui", "comfyui_workflow"):
        cc = cfg.vlm.comfyui
        if not cc.workflow:
            return Check(FAIL, "vlm",
                         "provider=comfyui 但缺少 vlm.comfyui.workflow（接口设置里选）")
        wf = cfg.resolve(cc.workflow)
        if not wf.exists():
            return Check(FAIL, "vlm", f"VLM 工作流文件不存在: {wf}（须为 API 格式 JSON）")
        reachable = False
        try:
            import requests

            r = requests.get(f"{cc.base_url}/system_stats", timeout=2)
            reachable = r.ok
        except Exception:  # noqa: BLE001
            reachable = False
        if reachable:
            return Check(OK, "vlm", f"ComfyUI 工作流理解 @ {cc.base_url}（{wf.name}）")
        return Check(WARN, "vlm", f"ComfyUI 未启动或不可达: {cc.base_url}")
    if cfg.vlm.api_key and "REPLACE" not in cfg.vlm.api_key:
        return Check(OK, "vlm", f"云端 VLM: {cfg.vlm.model or '?'} @ {cfg.vlm.base_url}")
    return Check(
        WARN, "vlm",
        "VLM 未配置 Key，画面理解降级为纯台词轨（config.toml [vlm]）",
    )


def _cublas_available() -> bool:
    """Windows 下检查 cublas64_*.dll 是否可被发现（系统/CUDA/PyPI nvidia 包）。"""
    import sys

    if sys.platform != "win32":
        return True    # 非 Windows 不做判断
    import os

    dirs: list[Path] = []
    dirs.append(Path(os.environ.get("SYSTEMROOT", r"C:\Windows")) / "System32")
    if os.environ.get("CUDA_PATH"):
        dirs.append(Path(os.environ["CUDA_PATH"]) / "bin")
    dirs += [Path(p) for p in os.environ.get("PATH", "").split(";") if p]
    try:
        import ctranslate2  # noqa: F401  仅用于定位其所在 site-packages

        sp = Path(ctranslate2.__file__).resolve().parent.parent
        dirs += [
            sp / "nvidia" / "cublas" / "bin",
            sp / "nvidia" / "cublas" / "lib",
        ]
    except Exception:  # noqa: BLE001
        pass
    for d in dirs:
        try:
            if any(d.glob("cublas64_*.dll")):
                return True
        except OSError:
            continue
    return False


def _asr_check(cfg: AppConfig) -> Check:
    a = cfg.asr
    if not a.enabled:
        return Check(OK, "asr", "已禁用（依赖外挂/内嵌字幕）")
    if a.engine == "comfyui":
        cc = a.comfyui
        if not cc.workflow:
            return Check(FAIL, "asr",
                         "engine=comfyui 但缺少 asr.comfyui.workflow（接口设置里选）")
        wf = cfg.resolve(cc.workflow)
        if not wf.exists():
            return Check(FAIL, "asr", f"ASR 工作流文件不存在: {wf}（须为 API 格式 JSON）")
        reachable = False
        try:
            import requests

            r = requests.get(f"{cc.base_url}/system_stats", timeout=2)
            reachable = r.ok
        except Exception:  # noqa: BLE001
            reachable = False
        if reachable:
            return Check(OK, "asr", f"ComfyUI 工作流转录 @ {cc.base_url}（{wf.name}）")
        return Check(WARN, "asr", f"ComfyUI 未启动或不可达: {cc.base_url}")
    if a.engine != "faster_whisper":
        return Check(FAIL, "asr", f"未知 ASR 引擎: {a.engine}（faster_whisper | comfyui | off）")
    if _has_module("faster_whisper"):
        if a.device != "cpu" and not _cublas_available():
            return Check(
                WARN, "asr",
                f"faster-whisper {a.device} 缺 cublas64_12.dll（转录会失败）→ "
                "装 nvidia-cublas-cu12，或 [asr] engine=\"comfyui\" 走工作流",
            )
        return Check(OK, "asr", f"faster-whisper {a.model} ({a.device})")
    return Check(WARN, "asr", "未安装 faster-whisper：uv sync --extra asr（否则需外挂字幕）")


def _workflow_cfg(cfg: AppConfig, engine: str):
    return cfg.tts.comfyui_clone if engine == "comfyui_clone" else cfg.tts.comfyui


def _tts_check(cfg: AppConfig) -> Check:
    engine = cfg.tts.engine
    if engine == "edge":
        if _has_module("edge_tts"):
            return Check(OK, "tts", f"edge-tts（{cfg.tts.default_voice}），需联网")
        return Check(WARN, "tts", "未安装 edge-tts：uv sync --extra tts")
    if engine in ("comfyui", "comfyui_clone"):
        wc = _workflow_cfg(cfg, engine)
        label = "克隆" if engine == "comfyui_clone" else "标准"
        if not wc.workflow:
            return Check(FAIL, "tts", f"ComfyUI {label} 引擎缺少 workflow 路径（config.toml）")
        wf = cfg.resolve(wc.workflow)
        if not wf.exists():
            return Check(FAIL, "tts", f"工作流文件不存在: {wf}（须为 API 格式 JSON）")
        reachable = False
        try:
            import requests

            r = requests.get(f"{wc.base_url}/system_stats", timeout=2)
            reachable = r.ok
        except Exception:  # noqa: BLE001
            reachable = False
        if reachable:
            return Check(OK, "tts", f"ComfyUI {label} @ {wc.base_url}（{wf.name}）")
        return Check(WARN, "tts", f"ComfyUI 未启动或不可达: {wc.base_url}")
    return Check(FAIL, "tts", f"未知 TTS 引擎: {engine}（edge | comfyui | comfyui_clone）")


def _ui_check() -> Check:
    if _has_module("PyQt6.QtWidgets"):
        return Check(OK, "ui", "PyQt6 可用")
    return Check(WARN, "ui", "未安装 PyQt6：uv sync --extra ui（CLI 仍可用）")


def _paths_check(cfg: AppConfig) -> Check:
    bad: list[str] = []
    for label, path in (("workspace", cfg.workspace_dir), ("output", cfg.output_dir)):
        try:
            path.mkdir(parents=True, exist_ok=True)
            probe = path / ".doctor_write_test"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
        except OSError:
            bad.append(f"{label}={path}")
    if bad:
        return Check(FAIL, "paths", "目录不可写: " + ", ".join(bad))
    return Check(OK, "paths", f"workspace={cfg.workspace_dir}  output={cfg.output_dir}")


def _font_check(cfg: AppConfig) -> Check:
    font = cfg.resolve(cfg.output.subtitle_font)
    if font.exists():
        return Check(OK, "font", str(font))
    if not cfg.output.subtitle:
        return Check(OK, "font", "字幕已关闭，无需字体")
    return Check(
        WARN, "font",
        f"字幕字体不存在: {font}（将回退系统字体，中文显示可能异常，建议放置中文字体）",
    )


def _unknown_keys_check(cfg: AppConfig, path: Path | None) -> Check | None:
    """配置文件里写了但模型不认识的键（拼写错误会被静默忽略）。"""
    if path is None or not path.exists():
        return None
    import tomllib
    from typing import get_args

    from pydantic import BaseModel

    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001  解析失败由 load_config 负责报错
        return None

    unknown: list[str] = []
    for k, v in raw.items():
        if k not in AppConfig.model_fields:
            unknown.append(str(k))
            continue
        ann = AppConfig.model_fields[k].annotation
        cls = ann if isinstance(ann, type) and issubclass(ann, BaseModel) else next(
            (a for a in get_args(ann) if isinstance(a, type) and issubclass(a, BaseModel)),
            None,
        )
        if cls is not None and isinstance(v, dict):
            unknown += [f"{k}.{kk}" for kk in v if kk not in cls.model_fields]
    if unknown:
        return Check(WARN, "config", "未知配置项（可能拼写错误）: " + ", ".join(unknown))
    return Check(OK, "config", f"配置项校验通过: {path.name}")


def run_checks(cfg: AppConfig, config_path: Path | None = None) -> list[Check]:
    """执行全部自检，返回检查结果列表。"""
    checks = [
        _ffmpeg_check(),
        _paths_check(cfg),
        _llm_check(cfg),
        _vlm_check(cfg),
        _asr_check(cfg),
        _tts_check(cfg),
        _ui_check(),
        _font_check(cfg),
    ]
    unknown = _unknown_keys_check(cfg, config_path)
    if unknown is not None:
        checks.append(unknown)
    return checks


_MARK = {OK: "  OK", WARN: " WARN", FAIL: "FAIL"}


def format_report(checks: list[Check]) -> str:
    lines = ["虎猫解说 自检报告", "-" * 64]
    lines += [f"[{_MARK[c.level]}] {c.name:<8} {c.message}" for c in checks]
    fails = sum(1 for c in checks if c.level == FAIL)
    warns = sum(1 for c in checks if c.level == WARN)
    lines.append("-" * 64)
    lines.append(f"通过 {sum(1 for c in checks if c.level == OK)}，"
                 f"警告 {warns}，失败 {fails}")
    return "\n".join(lines)
