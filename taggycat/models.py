"""虎猫解说 数据模型（Pydantic schema）。"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------
# 流水线阶段
# ---------------------------------------------------------------------
class StageName(str, Enum):
    """14 阶段流水线的稳定标识。"""

    PROBE = "probe"                    # 1 媒体探测
    SUBTITLE = "subtitle"              # 2 外挂/内嵌字幕
    ASR = "asr"                        # 3 台词转录
    FRAMES = "frames"                  # 4 抽帧 + 场景切分
    UNDERSTAND = "understand"          # 5 VLM 画面理解
    ANALYZE = "analyze"                # 6 剧情分析
    PLAN = "plan"                      # 7 分段规划（素材选取比例）
    SCRIPT = "script"                  # 8 解说文案生成
    MATCH = "match"                    # 9 文案↔画面匹配
    REVIEW = "review"                  # 10 文案校验（人工/自动）
    TTS = "tts"                        # 11 逐段配音
    SUBTITLE_GEN = "subtitle_gen"      # 12 字幕时间轴
    ASSEMBLE = "assemble"              # 13 FFmpeg 剪辑合成
    EXPORT = "export"                  # 14 成片导出


STAGE_ORDER: list[StageName] = [
    StageName.PROBE,
    StageName.SUBTITLE,
    StageName.ASR,
    StageName.FRAMES,
    StageName.UNDERSTAND,
    StageName.ANALYZE,
    StageName.PLAN,
    StageName.SCRIPT,
    StageName.MATCH,
    StageName.REVIEW,
    StageName.TTS,
    StageName.SUBTITLE_GEN,
    StageName.ASSEMBLE,
    StageName.EXPORT,
]


class StageStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"
    REVIEW = "review"      # 等待人工审核（校验文案）


# ---------------------------------------------------------------------
# 任务状态
# ---------------------------------------------------------------------
class StageState(BaseModel):
    """单阶段执行状态（落盘于 state.json，支持断点续跑）。"""

    name: str
    status: StageStatus = StageStatus.PENDING
    started_at: str | None = None
    finished_at: str | None = None
    error: str | None = None
    outputs: dict[str, str] = Field(default_factory=dict)   # key -> 相对路径
    data: dict[str, Any] = Field(default_factory=dict)      # 阶段自定义摘要


class SourceFile(BaseModel):
    """素材池中的一个视频文件。"""

    path: str                       # 绝对路径
    name: str                       # 展示名（文件名）
    size_mb: float = 0.0
    media_id: str = ""              # 稳定 ID（用于目录命名）


class ProjectConfig(BaseModel):
    """项目级「剪辑配置」（对应 UI 面板，创建任务时确定）。"""

    language: str = "中文"
    style: str = "电影解说"
    voice: str = ""                 # 配音音色（空=用全局默认）
    tts_engine: str = ""            # TTS 引擎覆盖（空=用全局 [tts].engine）
    rate: str = ""                  # 语速覆盖（edge rate，如 "+10%"；空=按风格语速）
    pov: str = ""                   # 叙述视角：""=跟随风格 | first | third
    pov_character: str = ""         # 第一人称讲述所代入的角色名（空=主角）
    tts_cache: bool = True          # 是否复用配音缓存
    bgm: str = ""                   # 背景音乐路径，空 = 无
    material_ratio: int = 30        # 素材选取比例 %（素材过剩时候选采样覆盖比例）
    narration_ratio: int = 50       # 解说比例 %
    target_duration: int = 300      # 目标成片时长（秒）
    validate_script: bool = True    # 校验文案（开=配音前人工审核）
    output_dir: str = "output"


class TaskState(BaseModel):
    """任务全量状态，落盘 workspace/<task_id>/state.json。"""

    task_id: str
    name: str
    created_at: str
    updated_at: str
    project: ProjectConfig
    sources: list[SourceFile] = Field(default_factory=list)
    stages: dict[str, StageState] = Field(default_factory=dict)
    current_stage: str | None = None
    finished: bool = False
    archived: bool = False           # 归档（默认列表隐藏）
    version: int = 1

    def stage(self, name: StageName | str) -> StageState:
        key = name.value if isinstance(name, StageName) else name
        if key not in self.stages:
            self.stages[key] = StageState(name=key)
        return self.stages[key]


def derive_task_state(t: TaskState) -> str:
    """派生任务展示状态（纯函数，无 Qt 依赖）。

    优先级：已归档 → 已完成 → 失败 → 待审核 → 运行中 → 等待中。
    """
    review = t.stage("review").status == StageStatus.REVIEW
    running = any(t.stage(s).status == StageStatus.RUNNING for s in STAGE_ORDER)
    failed = any(t.stage(s).status == StageStatus.FAILED for s in STAGE_ORDER)
    if t.archived:
        return "已归档"
    if t.finished:
        return "已完成"
    if failed:
        return "失败"
    if review:
        return "待审核"
    if running:
        return "运行中"
    return "等待中"


# ---------------------------------------------------------------------
# 媒体信息
# ---------------------------------------------------------------------
class SubtitleStreamInfo(BaseModel):
    index: int
    codec: str = ""
    language: str = ""
    title: str = ""


class MediaInfo(BaseModel):
    """ffprobe 媒体探测结果。"""

    path: str
    duration: float = 0.0
    size_bytes: int = 0
    width: int = 0
    height: int = 0
    fps: float = 0.0
    video_codec: str = ""
    audio_codec: str = ""
    has_audio: bool = False
    bitrate_kbps: int = 0
    subtitle_streams: list[SubtitleStreamInfo] = Field(default_factory=list)
    format_name: str = ""

    @property
    def aspect(self) -> str:
        if not self.width or not self.height:
            return ""
        g = _gcd(self.width, self.height)
        return f"{self.width // g}:{self.height // g}"


class SceneCut(BaseModel):
    """场景切分点。"""

    time_sec: float
    score: float = 0.0


# ---------------------------------------------------------------------
# 通用
# ---------------------------------------------------------------------
class Segment(BaseModel):
    """规划出的单个剪辑片段（跨素材池）。"""

    segment_id: str
    source_id: str = ""             # 对应 SourceFile.media_id
    start: float = 0.0              # 入点（秒）
    end: float = 0.0                # 出点（秒）
    score: float = 0.0              # 高光评分
    reason: str = ""
    transcript: str = ""            # 该段台词
    caption: str = ""               # 解说文案
    audio_path: str = ""            # TTS 产物
    voice_duration: float = 0.0     # 配音音频时长（秒）
    duration: float = 0.0           # 成片中该段时长 = max(clip, voice+留白)
    keep_audio: bool = False        # 原声让路片段：整段保留原声、不配音、不出字幕
    plan_yield: bool = False        # 规划点名让路：原声台词完整露出以配合解说（为讲解服务）

    @property
    def clip_duration(self) -> float:
        return max(0.0, self.end - self.start)


def _gcd(a: int, b: int) -> int:
    while b:
        a, b = b, a % b
    return a or 1
