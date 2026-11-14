"""配置加载：config.toml → Pydantic 模型；键路径级写回（保留注释）。"""

from __future__ import annotations

import os
import re
import shutil
import sys
import tomllib
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from loguru import logger
from pydantic import BaseModel, Field

from taggycat.paths import resolve_path


class LLMConfig(BaseModel):
    provider: str = "openai_compatible"
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    temperature: float = 0.7
    max_tokens: int = 4096
    timeout: int = 180
    max_retries: int = 3
    # 推理模型思考控制：none|low|medium|high（none=完全关闭，限制生成长度）
    # 空=不传；服务端不支持时自动去掉该参数重试
    reasoning_effort: str = ""


class ComfyEndpointConfig(BaseModel):
    """ComfyUI 工作流端点公共配置。"""

    base_url: str = "http://127.0.0.1:8188"
    workflow: str = ""              # API 格式 JSON
    timeout: int = 600
    poll_interval: float = 1.0


class ComfyVLMConfig(ComfyEndpointConfig):
    """ComfyUI 视觉理解工作流（vlm.provider = "comfyui" 时启用）。"""

    image_input: str = ""           # 图像输入名（空=自动识别 LoadImage 节点）
    text_input: str = ""            # 提示词输入名（空=自动识别；找不到则用工作流内置提示词）


class VLMConfig(BaseModel):
    enabled: bool = True
    provider: str = "openai_compatible"   # openai_compatible | comfyui
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    max_frames: int = 80
    frame_interval: float = 10.0
    scene_first: bool = True
    timeout: int = 180
    max_retries: int = 3
    comfyui: ComfyVLMConfig = Field(default_factory=ComfyVLMConfig)


class ComfyASRConfig(ComfyEndpointConfig):
    """ComfyUI 语音转录工作流（asr.engine = "comfyui" 时启用）。"""

    audio_input: str = ""           # 音频输入名（空=自动识别 LoadAudio 节点）
    max_upload_mb: float = 90.0     # 单次上传上限，超出自动分段（ComfyUI 默认 100MB）
    timeout: int = 1800             # 整条音轨转录可能较慢


class ASRConfig(BaseModel):
    enabled: bool = True
    engine: str = "faster_whisper"  # faster_whisper | comfyui | off
    model: str = "large-v3"
    device: str = "cuda"
    compute_type: str = "float16"
    language: str = "zh"
    beam_size: int = 5
    vad_filter: bool = True
    comfyui: ComfyASRConfig = Field(default_factory=ComfyASRConfig)


class TTSEdgeConfig(BaseModel):
    voice: str = "zh-CN-YunxiNeural"
    rate: str = "+0%"
    volume: str = "+0%"


class TTSComfyConfig(ComfyEndpointConfig):
    reference_audio: str = ""
    text_input: str = ""            # 工作流中承载文本的输入名（空=自动识别）
    ref_input: str = ""             # 承载参考音频的输入名（克隆，空=自动识别）


class TTSConfig(BaseModel):
    engine: str = "edge"            # edge | comfyui | comfyui_clone
    default_voice: str = "zh-CN-YunxiNeural"
    cache: bool = True              # 配音音频缓存（同文案不重复合成）
    max_workers: int = 4            # 未命中缓存段的并发合成上限
    sample_rate: int = 44100
    edge: TTSEdgeConfig = Field(default_factory=TTSEdgeConfig)
    comfyui: TTSComfyConfig = Field(default_factory=TTSComfyConfig)
    comfyui_clone: TTSComfyConfig = Field(default_factory=TTSComfyConfig)


class MaterialConfig(BaseModel):
    target_duration: int = 300
    material_ratio: int = 30
    narration_ratio: int = 50
    clip_min_duration: float = 4.0
    clip_max_duration: float = 12.0
    scene_threshold: float = 0.30


class OutputConfig(BaseModel):
    language: str = "中文"
    style: str = "电影解说"
    width: int = 1920
    height: int = 1080
    fps: int = 30
    video_codec: str = "libx264"
    audio_codec: str = "aac"
    crf: int = 21
    preset: str = "medium"
    audio_bitrate: str = "192k"
    bgm_volume: float = 0.15
    keep_original_audio: bool = True
    original_audio_volume: float = 0.3
    # 原声处理：keep=保留 | mute=全消音 | tail=仅段尾保留一小段（空=跟随 keep_original_audio）
    original_audio_mode: str = ""
    original_audio_tail: float = 1.2   # tail 模式段尾保留秒数
    # 原声台词让路（为讲解服务）：仅规划阶段点名 plan_yield 的片段，
    # 且台词重叠 ≥ max(voice_yield_min, 片段时长一半) 时才整段保留原声；
    # 未点名的片段绝不让路（防止原声原字幕随便露出）
    voice_yield: bool = False
    voice_yield_min: float = 2.0
    # 讲解配音音高对齐：逐段估计原声与配音的基频差，各自移调配音使其音高接近原声
    #（时长不变；幅度逐段钳制于 voice_pitch_max_semitones）
    voice_pitch_match: bool = True
    voice_pitch_max_semitones: float = 3.0
    subtitle: bool = True
    subtitle_font: str = "resources/fonts/NotoSansSC-Regular.otf"
    subtitle_fontsize: int = 64
    # 双语字幕：中文字幕下加一行译文（LLM 翻译，带缓存；失败自动降级单语）
    subtitle_bilingual: bool = False
    subtitle_lang2: str = "en"          # 副语言：en/ja/ko/fr/de/es…
    # 原片烧录字幕遮盖：讲解段遮盖底部字幕带（blur=高斯模糊 / delogo=插值抹除），
    # 原声让路段保留原字幕
    hard_sub_remove: bool = True
    hard_sub_band: str = "0.04,0.96,0.80,1.00"  # 遮盖带 x1,x2,y1,y2（画面宽高占比）
    hard_sub_mode: str = "blur"     # blur | delogo

    @property
    def effective_hard_sub_mode(self) -> str:
        """生效的遮盖方式（blur | delogo）。"""
        return "delogo" if self.hard_sub_mode.strip().lower() == "delogo" else "blur"

    @property
    def effective_original_audio(self) -> str:
        """解析生效的原声模式（keep | mute | tail）。"""
        mode = (self.original_audio_mode or "").strip().lower()
        if mode in ("keep", "mute", "tail"):
            return mode
        return "keep" if self.keep_original_audio else "mute"

    def hard_sub_rect(self) -> tuple[int, int, int, int] | None:
        """讲解段遮盖原片字幕的像素矩形 (x, y, w, h)；解析失败/越界返回 None。"""
        try:
            x1, x2, y1, y2 = (float(v) for v in self.hard_sub_band.split(","))
        except (ValueError, AttributeError):
            return None
        if not (0.0 <= x1 < x2 <= 1.0 and 0.0 <= y1 < y2 <= 1.0):
            return None
        x = max(1, int(self.width * x1))
        y = max(1, int(self.height * y1))
        w = min(int(self.width * (x2 - x1)), self.width - x - 1)
        h = min(int(self.height * (y2 - y1)), self.height - y - 1)
        if w < 2 or h < 2:
            return None
        return x, y, w, h


class ReviewConfig(BaseModel):
    validate_script: bool = True


class QueueConfig(BaseModel):
    max_concurrent: int = 1


class PathsConfig(BaseModel):
    workspace: str = "workspace"
    output: str = "output"
    fonts: str = "resources/fonts"
    bgm_dir: str = "resources/songs"
    voice_samples: str = "resources/voices"
    tts_cache: str = "workspace/tts_cache"


class UIConfig(BaseModel):
    theme: str = "light"  # light | light-green | light-purple | dark | dark-amber


class AppConfig(BaseModel):
    llm: LLMConfig = Field(default_factory=LLMConfig)
    llm_local: LLMConfig = Field(default_factory=LLMConfig)
    vlm: VLMConfig = Field(default_factory=VLMConfig)
    asr: ASRConfig = Field(default_factory=ASRConfig)
    tts: TTSConfig = Field(default_factory=TTSConfig)
    material: MaterialConfig = Field(default_factory=MaterialConfig)
    output: OutputConfig = Field(default_factory=OutputConfig)
    review: ReviewConfig = Field(default_factory=ReviewConfig)
    queue: QueueConfig = Field(default_factory=QueueConfig)
    paths: PathsConfig = Field(default_factory=PathsConfig)
    ui: UIConfig = Field(default_factory=UIConfig)

    # 运行期解析出的根目录（配置文件所在目录或 CWD）
    root_dir: Path = Field(default_factory=Path.cwd)
    # 实际加载的配置文件路径（供设置对话框写回；不会来自 TOML 本身）
    config_path: Path | None = None

    def resolve(self, p: str | Path) -> Path:
        """相对路径基于项目根目录解析；resources/ 随包资源可回退到 bundle 根。

        运行时产物（workspace/、output/）始终写入配置所在目录，不会被导向
        PyInstaller 的临时解压目录。详见 taggycat.paths.resolve_path。
        """
        return resolve_path(p, self.root_dir)

    @property
    def workspace_dir(self) -> Path:
        return self.resolve(self.paths.workspace)

    @property
    def output_dir(self) -> Path:
        return self.resolve(self.paths.output)

    @property
    def effective_llm(self) -> LLMConfig:
        """按 provider 选择主/备 LLM。"""
        if self.llm.provider and self.llm.api_key and "REPLACE" not in self.llm.api_key:
            return self.llm
        if self.llm_local.base_url:
            logger.warning("主 LLM 未配置有效 API Key，回退到本地 LLM: {}", self.llm_local.model)
            return self.llm_local
        return self.llm


def find_config(explicit: str | Path | None = None) -> Path:
    """定位配置文件：显式路径 > TAGGYCAT_CONFIG > ./config.toml > ./config.example.toml。

    兼容旧版环境变量 CLIPNARRA_CONFIG（0.1.0 之前），仅在新变量未设置时生效。
    """
    if explicit:
        p = Path(explicit).expanduser().resolve()
        if not p.exists():
            raise FileNotFoundError(f"配置文件不存在: {p}")
        return p

    env_name = "TAGGYCAT_CONFIG"
    env = os.environ.get(env_name)
    if not env:
        legacy = os.environ.get("CLIPNARRA_CONFIG")
        if legacy:
            env_name, env = "CLIPNARRA_CONFIG", legacy
            logger.warning("CLIPNARRA_CONFIG 已弃用，请改用 TAGGYCAT_CONFIG")
    if env:
        p = Path(env).expanduser().resolve()
        if not p.exists():
            raise FileNotFoundError(f"{env_name} 指向的文件不存在: {p}")
        return p

    # 单文件 exe：以 exe 所在目录为项目根（双击启动时 CWD 未必是它）。
    # 首次运行自动从 bundle 复制配置模板，用户可在「接口设置」里写回。
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        local = exe_dir / "config.toml"
        if local.is_file():
            return local
        bundle = getattr(sys, "_MEIPASS", None)
        template = (Path(bundle) if bundle else exe_dir) / "config.example.toml"
        if template.is_file():
            try:
                shutil.copy2(template, local)
                logger.info("已生成默认配置: {}", local)
                return local
            except OSError:
                logger.warning("配置目录不可写，使用只读模板: {}", template)
                return template

    for name in ("config.toml", "config.example.toml"):
        p = Path.cwd() / name
        if p.exists():
            return p.resolve()

    raise FileNotFoundError(
        "未找到配置文件。请执行 `cp config.example.toml config.toml` 并填入 API Key。"
    )


def load_config(path: str | Path | None = None) -> AppConfig:
    """加载 TOML 配置为 AppConfig。"""
    cfg_path = find_config(path)
    with cfg_path.open("rb") as f:
        raw: dict[str, Any] = tomllib.load(f)

    config = AppConfig.model_validate(raw)
    # 根目录取配置文件所在目录（便于从任意 CWD 启动）
    config.root_dir = cfg_path.parent
    config.config_path = cfg_path
    logger.debug("已加载配置: {}", cfg_path)
    return config


# ---------------------------------------------------------------------
# TOML 写回（按键路径更新单个键，保留注释与其余内容）
# ---------------------------------------------------------------------
_HEADER_RE = re.compile(r"^\s*\[([^\]]+)\]")


def format_toml_value(value: Any) -> str:
    """把 Python 值序列化为 TOML 字面量（字符串转义反斜杠与引号）。"""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, str):
        esc = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{esc}"'
    raise TypeError(f"不支持写入 TOML 的类型: {type(value).__name__}")


def _apply_key(lines: list[str], key: str, value: Any) -> list[str]:
    """按 `a.b.c` 键路径更新行列表；表不存在/键不存在时自动补建。"""
    parts = [p for p in key.split(".") if p]
    if not parts:
        raise ValueError("键路径为空")
    fmt = format_toml_value(value)

    headers: list[tuple[int, str]] = []
    for i, line in enumerate(lines):
        m = _HEADER_RE.match(line)
        if m:
            headers.append((i, m.group(1)))

    # 最长前缀匹配已有表头：键 "tts.comfyui.workflow" → 表 [tts.comfyui] + 键 workflow
    hdr: str | None = None
    hdr_idx: int | None = None
    match_k = 0
    for k in range(len(parts) - 1, 0, -1):
        cand = ".".join(parts[:k])
        for idx, name in headers:
            if name == cand:
                hdr, hdr_idx, match_k = cand, idx, k
                break
        if hdr is not None:
            break
    if hdr is None:
        if len(parts) == 1:
            match_k = 0                       # 顶层键（无表头）
        else:
            match_k = len(parts) - 1          # 表头整段缺失 → 末尾补建
            hdr = ".".join(parts[:-1])
    keyname = ".".join(parts[match_k:])
    assign_re = re.compile(r"^\s*" + re.escape(keyname) + r"\s*=")

    # 计算所属块 [start, end)
    if hdr is None:
        start = 0
        end = headers[0][0] if headers else len(lines)
    elif hdr_idx is not None:
        start = hdr_idx + 1
        nxt = [i for i, _ in headers if i > hdr_idx]
        end = nxt[0] if nxt else len(lines)
    else:
        # 表不存在：文件末尾补建
        out = list(lines)
        while out and not out[-1].strip():
            out.pop()
        out += ["", f"[{hdr}]", f"{keyname} = {fmt}"]
        return out

    for i in range(start, end):
        if assign_re.match(lines[i]):
            line = lines[i]
            eq = line.index("=")
            lhs, rhs = line[: eq + 1], line[eq + 1:]
            comment = ""
            j = rhs.find(" #")
            if j != -1:
                comment = rhs[j:]
            out = list(lines)
            out[i] = f"{lhs} {fmt}{comment}"
            return out

    # 键不在块内：插到块尾（贴住最后一个非空行）
    insert = end
    for i in range(end - 1, start - 1, -1):
        if lines[i].strip():
            insert = i + 1
            break
    out = list(lines)
    out.insert(insert, f"{keyname} = {fmt}")
    return out


def set_config_values(path: Path | str, items: Iterable[tuple[str, Any]]) -> None:
    """批量按键路径写回 TOML 文件（单次读写，保留注释）。"""
    path = Path(path)
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    lines = text.splitlines()
    for key, value in items:
        lines = _apply_key(lines, key, value)
    out = "\n".join(lines)
    if text.endswith("\n"):
        out += "\n"
    path.write_text(out, encoding="utf-8")


def set_config_value(path: Path | str, key: str, value: Any) -> None:
    """按键路径写回单个键，如 set_config_value(p, "llm.api_key", "sk-…")。"""
    set_config_values(path, [(key, value)])
