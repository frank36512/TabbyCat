"""剧情时间线：台词（ASR/字幕）× VLM 画面描述 → 统一时间轴。"""

from __future__ import annotations

from pathlib import Path

from loguru import logger
from pydantic import BaseModel, Field

from taggycat.asr import Transcript
from taggycat.models import SceneCut


class TimelineEntry(BaseModel):
    start: float
    end: float
    dialog: str = ""          # 该时间窗内的台词
    visual: str = ""          # 该时间窗内的画面描述
    frame_times: list[float] = Field(default_factory=list)

    @property
    def summary(self) -> str:
        parts = []
        if self.visual:
            parts.append(f"[画面] {self.visual}")
        if self.dialog:
            parts.append(f"[台词] {self.dialog}")
        return " ".join(parts)


class Timeline(BaseModel):
    media_id: str
    duration: float = 0.0
    entries: list[TimelineEntry] = Field(default_factory=list)
    cut_times: list[float] = Field(default_factory=list)

    @property
    def text(self) -> str:
        return "\n".join(e.summary for e in self.entries if e.summary)

    def save(self, path: str | Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.model_dump_json(indent=2), encoding="utf-8")
        return path

    @classmethod
    def load(cls, path: str | Path) -> Timeline:
        return cls.model_validate_json(Path(path).read_text(encoding="utf-8"))


def build_timeline(
    media_id: str,
    duration: float,
    *,
    transcript: Transcript | None = None,
    frame_descs: list[dict] | None = None,
    cuts: list[SceneCut] | list[float] | None = None,
    bucket: float = 30.0,
) -> Timeline:
    """按固定时间桶（bucket 秒）聚合台词与画面描述。

    - transcript: ASR/字幕台词
    - frame_descs: [{"t": 秒, "desc": str}, ...]
    - cuts: 场景切点（额外作为分桶边界）
    """
    # 分桶边界：均匀 bucket + 场景切点
    bounds: list[float] = [0.0]
    cut_list: list[float] = []
    if cuts:
        for c in cuts:
            t = c.time_sec if isinstance(c, SceneCut) else float(c)
            if t > 0:
                cut_list.append(t)
    t = bucket
    while t < duration - 0.1:
        bounds.append(t)
        t += bucket
    bounds.extend(sorted(set(cut_list)))
    bounds.append(max(duration, 0.1))
    bounds = sorted(set(bounds))

    # 画面 desc 按时间索引
    visuals: list[tuple[float, str]] = []
    for d in frame_descs or []:
        try:
            visuals.append((float(d.get("t", 0.0)), str(d.get("desc", ""))))
        except (TypeError, ValueError):
            continue
    visuals.sort(key=lambda x: x[0])

    entries: list[TimelineEntry] = []
    for i in range(len(bounds) - 1):
        s, e = bounds[i], bounds[i + 1]
        if e - s < 0.05:
            continue
        dialogs = [
            it.text for it in (transcript.items if transcript else [])
            if it.end > s and it.start < e
        ]
        vis = [desc for t, desc in visuals if s <= t < e and desc]
        fts = [t for t, _ in visuals if s <= t < e]
        if not dialogs and not vis:
            continue
        entries.append(
            TimelineEntry(
                start=s, end=e,
                dialog=" ".join(d.strip() for d in dialogs),
                visual="；".join(vis),
                frame_times=fts,
            )
        )

    tl = Timeline(
        media_id=media_id, duration=duration, entries=entries,
        cut_times=cut_list,
    )
    logger.debug("时间线: {} 段 (bucket={}s)", len(entries), bucket)
    return tl
