"""segments.json 统一读写入口（保证序列化格式一致）。"""

from __future__ import annotations

import json
from pathlib import Path

from taggycat.models import Segment


def load_segments(path: str | Path) -> list[Segment]:
    """从 JSON 文件读取片段列表。"""
    return [
        Segment.model_validate(x)
        for x in json.loads(Path(path).read_text(encoding="utf-8"))
    ]


def write_segments(path: str | Path, segs: list[Segment]) -> Path:
    """把片段列表以统一格式写入 JSON 文件，返回文件路径。"""
    p = Path(path)
    p.write_text(
        json.dumps([s.model_dump() for s in segs], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return p
