"""SRT 字幕解析与生成（自实现，无外部依赖）。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Cue:
    index: int
    start: float          # 秒
    end: float
    text: str

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


@dataclass
class Subtitle:
    cues: list[Cue] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "\n".join(c.text for c in self.cues)

    @property
    def duration(self) -> float:
        return self.cues[-1].end if self.cues else 0.0

    def shift(self, delta: float) -> Subtitle:
        for c in self.cues:
            c.start = max(0.0, c.start + delta)
            c.end = max(0.0, c.end + delta)
        return self

    def slice(self, start: float, end: float) -> Subtitle:
        """截取 [start, end) 内的字幕，时间轴归零到 start。"""
        out: list[Cue] = []
        for c in self.cues:
            if c.end <= start or c.start >= end:
                continue
            s = max(c.start, start) - start
            e = min(c.end, end) - start
            out.append(Cue(index=len(out) + 1, start=s, end=e, text=c.text))
        return Subtitle(cues=out)


_TS_RE = re.compile(r"(\d+):(\d{2}):(\d{2})[,.](\d{1,3})")


def parse_ts(raw: str) -> float:
    """`HH:MM:SS,mmm` → 秒。"""
    m = _TS_RE.search(raw.strip())
    if not m:
        raise ValueError(f"非法时间戳: {raw!r}")
    h, mi, s, ms = m.groups()
    return int(h) * 3600 + int(mi) * 60 + int(s) + int(ms.ljust(3, "0")) / 1000.0


def format_ts(sec: float) -> str:
    """秒 → `HH:MM:SS,mmm`。"""
    if sec < 0:
        sec = 0.0
    ms = int(round(sec * 1000))
    h, rem = divmod(ms, 3_600_000)
    mi, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{mi:02d}:{s:02d},{ms:03d}"


def parse_srt(content: str) -> Subtitle:
    cues: list[Cue] = []
    blocks = re.split(r"\n\s*\n", content.replace("\r\n", "\n").strip())
    for block in blocks:
        lines = [ln for ln in block.split("\n") if ln.strip() != ""]
        if len(lines) < 2:
            continue
        # 首行可能是序号
        idx_line = 0
        idx = len(cues) + 1
        if lines[0].strip().isdigit():
            idx = int(lines[0].strip())
            idx_line = 1
        if idx_line >= len(lines):
            continue
        ts = lines[idx_line]
        if "-->" not in ts:
            continue
        raw_start, _, raw_end = ts.partition("-->")
        try:
            start = parse_ts(raw_start)
            end = parse_ts(raw_end)
        except ValueError:
            continue
        text = "\n".join(lines[idx_line + 1:])
        cues.append(Cue(index=idx, start=start, end=end, text=text))
    return Subtitle(cues=cues)


def read_srt(path: str | Path) -> Subtitle:
    raw = Path(path).read_bytes()
    for enc in ("utf-8-sig", "utf-8", "gbk", "gb18030"):
        try:
            return parse_srt(raw.decode(enc))
        except UnicodeDecodeError:
            continue
    return parse_srt(raw.decode("utf-8", errors="replace"))


def write_srt(sub: Subtitle, path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    parts: list[str] = []
    for i, c in enumerate(sub.cues, start=1):
        parts.append(
            f"{i}\n{format_ts(c.start)} --> {format_ts(c.end)}\n{c.text}\n"
        )
    path.write_text("\n".join(parts), encoding="utf-8")
    return path


# ---------------------------------------------------------------------
# ASS（双语烧录：主行原字号，译文行缩小字号）
# ---------------------------------------------------------------------
def _ass_ts(sec: float) -> str:
    """秒 → ASS 时间戳 `H:MM:SS.cc`。"""
    cs = int(round(max(0.0, sec) * 100))
    h, rem = divmod(cs, 360_000)
    mi, rem = divmod(rem, 6_000)
    s, cs = divmod(rem, 100)
    return f"{h}:{mi:02d}:{s:02d}.{cs:02d}"


def _ass_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}")


def write_bilingual_ass(
    sub: Subtitle,
    path: str | Path,
    *,
    width: int,
    height: int,
    font_size: int,
    font2_ratio: float = 0.65,
    font_name: str = "sans-serif",
) -> Path:
    """双语 ASS：cue.text 首行为中文字幕（font_size），其余行为译文（缩小）。

    单事件 `\\N` 换行 + 行内 `\\fs` 标签，libass 按各自字号排版（译文行更小）。
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    en_fs = max(10, int(round(font_size * font2_ratio)))
    lines = [
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {width}",
        f"PlayResY: {height}",
        "WrapStyle: 0",
        "ScaledBorderAndShadow: yes",
        "",
        "[V4+ Styles]",
        ("Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
         "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
         "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
         "Alignment, MarginL, MarginR, MarginV, Encoding"),
        (f"Style: CN,{font_name},{font_size},&H00FFFFFF,&H00FFFFFF,"
         f"&H00000000,&H7F000000,0,0,0,0,100,100,0,0,1,2,0,2,40,40,36,1"),
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, "
        "Effect, Text",
    ]
    for c in sub.cues:
        parts = c.text.split("\n")
        zh = _ass_escape(parts[0].strip())
        en = _ass_escape(" ".join(p.strip() for p in parts[1:] if p.strip()))
        txt = zh + (f"\\N{{\\fs{en_fs}}}{en}" if en else "")
        lines.append(
            f"Dialogue: 0,{_ass_ts(c.start)},{_ass_ts(c.end)},CN,,0,0,0,,{txt}"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------------
# 口播字幕切分（用户要求口播字幕不带标点）
# ---------------------------------------------------------------------
# 字幕单条最大字数
CUE_MAX_CHARS = 18
# 字幕里要剔除的标点/符号
CAPTION_PUNCT = set("，。！？；、,.!?;：:“”\"'‘’…—–~·《》〈〉【】〔〕（）()[]{}　")


def strip_caption_punct(text: str) -> str:
    """去掉标点符号并折叠空白（TTS 合成仍用原文，标点利于断句停顿）。"""
    s = "".join(ch for ch in text if ch not in CAPTION_PUNCT)
    return " ".join(s.split())


def split_caption(text: str, max_chars: int = CUE_MAX_CHARS) -> list[str]:
    """按标点与最大字数把解说文案切成字幕条（输出不含标点）。"""
    parts: list[str] = []
    buf = ""
    for ch in text:
        buf += ch
        if ch in "，。！？；,.!?;、\n" or len(buf) >= max_chars:
            parts.append(strip_caption_punct(buf))
            buf = ""
    tail = strip_caption_punct(buf)
    if tail:
        parts.append(tail)
    return [p for p in parts if p]
