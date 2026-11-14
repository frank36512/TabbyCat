"""SRT 解析与生成测试。"""

import pytest

from taggycat.media.sub import format_ts, parse_srt, parse_ts, write_srt

SAMPLE = """1
00:00:01,000 --> 00:00:03,500
你好，世界

2
00:00:04,200 --> 00:00:06,000
第二行字幕
带换行
"""


def test_parse_ts():
    assert parse_ts("00:00:01,000") == 1.0
    assert parse_ts("01:02:03.500") == 3723.5
    assert parse_ts("00:00:00,010") == 0.01


def test_format_ts_roundtrip():
    assert format_ts(1.0) == "00:00:01,000"
    assert format_ts(3723.5) == "01:02:03,500"
    assert format_ts(-1) == "00:00:00,000"
    assert parse_ts(format_ts(123.456)) == pytest.approx(123.456)


def test_parse_srt():
    sub = parse_srt(SAMPLE)
    assert len(sub.cues) == 2
    assert sub.cues[0].text == "你好，世界"
    assert sub.cues[0].duration == pytest.approx(2.5)
    assert "带换行" in sub.cues[1].text
    assert sub.duration == pytest.approx(6.0)


def test_write_roundtrip(tmp_path):
    sub = parse_srt(SAMPLE)
    out = write_srt(sub, tmp_path / "out.srt")
    from taggycat.media.sub import read_srt

    sub2 = read_srt(out)
    assert len(sub2.cues) == 2
    assert sub2.cues[0].start == pytest.approx(1.0)
    assert sub2.cues[1].text == sub.cues[1].text


def test_slice_and_shift():
    sub = parse_srt(SAMPLE)
    part = sub.slice(2.0, 5.0)
    # cue1 (1.0-3.5) 与 [2,5) 相交被裁剪；cue2 (4.2-6.0) 也被裁剪
    assert len(part.cues) == 2
    assert part.cues[0].start == pytest.approx(0.0)     # 2.0 - 2.0
    assert part.cues[0].end == pytest.approx(1.5)       # 3.5 - 2.0
    assert part.cues[1].start == pytest.approx(2.2)     # 4.2 - 2.0
    part.shift(1.0)
    assert part.cues[1].start == pytest.approx(3.2)
    # 不相交的切片为空
    assert sub.slice(7.0, 9.0).cues == []


def test_empty_and_garbage():
    assert parse_srt("").cues == []
    assert parse_srt("乱七八糟").cues == []


def test_write_bilingual_ass(tmp_path):
    from taggycat.media.sub import Cue, Subtitle, write_bilingual_ass

    sub = Subtitle(cues=[
        Cue(index=1, start=0.5, end=2.25, text="中文行\nEnglish line"),
        Cue(index=2, start=3.0, end=4.0, text="仅中文"),
    ])
    p = write_bilingual_ass(sub, tmp_path / "bi.ass", width=1920, height=1080,
                            font_size=36, font_name="Microsoft YaHei")
    t = p.read_text(encoding="utf-8")
    assert "PlayResX: 1920" in t and "PlayResY: 1080" in t
    assert "Style: CN,Microsoft YaHei,36," in t
    # 译文行缩小字号（round(36*0.65)=23）+ \N 换行
    assert "中文行\\N{\\fs23}English line" in t
    assert "0:00:00.50" in t and "0:00:02.25" in t
    # 无译文 → 不加 \N
    assert "仅中文\n" in t or t.splitlines()[-1].endswith(",仅中文")
    assert "\\fs" not in t.splitlines()[-1]
    # 转义花括号防样式注入
    sub2 = Subtitle(cues=[Cue(index=1, start=0.0, end=1.0, text="{\\b1}坏")])
    p2 = write_bilingual_ass(sub2, tmp_path / "esc.ass", width=640, height=480,
                             font_size=24)
    assert "\\{\\\\b1\\}" in p2.read_text(encoding="utf-8")
