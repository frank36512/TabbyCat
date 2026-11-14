"""解说风格预设测试：23 种风格字段齐全、精确/模糊匹配、风格卡注入各阶段提示词。"""

from taggycat import prompts as P
from taggycat.prompts import STYLE_PRESETS, get_style

EXPECTED_STYLES = [
    "电影解说", "短剧漫剧", "悬疑解说", "吐槽解说",
    "产品解说", "人物解说", "地理解说", "知识解说",
    "搞笑式解说", "温柔式解说", "盘点式解说", "宣传式解说",
    "批判式解说", "聊天式解说", "激情式解说",
    "纪录片解说", "游戏解说", "体育赛事解说", "美食解说",
    "历史解说", "军事解说", "财经解说", "情感故事解说",
]


def test_all_expected_styles_present():
    assert len(STYLE_PRESETS) == len(EXPECTED_STYLES)
    for name in EXPECTED_STYLES:
        assert name in STYLE_PRESETS, f"缺少风格: {name}"


def test_presets_have_required_fields():
    for key, p in STYLE_PRESETS.items():
        assert p.name == key
        assert p.tone and p.structure and p.sample
        # 结构需含阶段箭头，示例是可朗读的中文口语句
        assert "→" in p.structure
        assert len(p.sample) >= 10


def test_presets_have_full_style_traits():
    """每种风格都应具备可落地到提示词的特征维度（对标同类工具的风格模板）。"""
    for key, p in STYLE_PRESETS.items():
        for field in ("pov", "hook", "pacing", "diction", "devices",
                      "avoid", "selection", "voice"):
            assert getattr(p, field), f"{key} 缺字段 {field}"
        assert p.voice.startswith("zh-CN-"), f"{key} 推荐音色异常"


def test_presets_have_rate_hooks_endings():
    """每种风格都应有语速、开场钩子模板库与结尾模板库。"""
    for key, p in STYLE_PRESETS.items():
        assert p.rate and p.rate > 0, f"{key} 缺语速"
        assert len(p.hooks) >= 2, f"{key} 开场钩子模板不足"
        assert len(p.endings) >= 2, f"{key} 结尾模板不足"
        assert all(h.strip() for h in p.hooks)
        assert all(e.strip() for e in p.endings)


def test_style_traits_are_distinct():
    """风格特征必须彼此不同，才能让同一素材产出不同风格。"""
    for field in ("hook", "pacing", "diction", "selection"):
        values = [getattr(p, field) for p in STYLE_PRESETS.values()]
        assert len(set(values)) == len(STYLE_PRESETS), f"字段 {field} 存在重复"


def test_get_style_exact_match():
    for name in EXPECTED_STYLES:
        assert get_style(name).name == name


def test_get_style_fuzzy_and_fallback():
    assert get_style("聊天式").name == "聊天式解说"     # 名称片段模糊命中
    assert get_style("短剧").name == "短剧漫剧"
    assert get_style("").name == "电影解说"             # 空串回退默认
    assert get_style("不存在的风格xyz").name == "电影解说"  # 未知回退默认


def test_render_style_skips_blank_fields():
    style = get_style("电影解说")
    card = P.render_style(style, "tone", "hook")
    assert style.tone in card
    assert style.hook in card
    assert style.structure not in card        # 未请求的维度不出现
    # 空维度被跳过：构造一个只填必填字段的风格
    bare = P.StylePreset(name="x", tone="t", structure="a → b", sample="示例文本内容")
    assert P.render_style(bare, "hook", "selection") == ""


def test_style_card_and_sample_per_stage():
    script = P.style_card(get_style("吐槽解说"), "script")
    assert get_style("吐槽解说").diction in script
    assert P.style_sample(get_style("吐槽解说")).find(get_style("吐槽解说").sample) != -1
    select = P.style_card(get_style("悬疑解说"), "select")
    assert get_style("悬疑解说").selection in select


def test_style_rate_and_edge_rate_mapping():
    """语速换算：基准 4.5 字/秒 = +0%，快慢风格落在合理区间。"""
    base = P.StylePreset(name="b", tone="t", structure="a → b", sample="示例文本内容")
    assert P.style_rate(base) == P.DEFAULT_SPEECH_RATE
    assert P.style_edge_rate(base) == "+0%"
    # 电影解说用基准语速
    assert P.style_edge_rate(get_style("电影解说")) == "+0%"
    # 悬疑更慢（负向），短剧更快（正向）
    assert P.style_edge_rate(get_style("悬疑解说")).startswith("-")
    assert P.style_edge_rate(get_style("短剧漫剧")).startswith("+")


def test_style_hooks_and_endings_render():
    style = get_style("电影解说")
    assert style.hooks[0] in P.style_hooks(style)
    assert style.endings[0] in P.style_endings(style)


def test_pov_line_modes():
    style = get_style("电影解说")
    # 默认跟随风格自带视角
    assert P.pov_line(style) == style.pov
    first = P.pov_line(style, "first", "阿强")
    assert "第一人称" in first and "阿强" in first
    third = P.pov_line(style, "third")
    assert "第三人称" in third


def test_style_cards_injected_into_prompt_templates():
    """各阶段提示词都能渲染风格卡，且风格特征确实进入最终提示词。"""
    style = get_style("知识解说")

    plan_user = P.PLAN_USER.format(
        style=style.name, style_card=P.style_card(style, "select"),
        source_total=60, count=5, budget=30, min_len=4, max_len=12,
        analysis="{}", candidates="c001 | a.mp4 | 0-5s | 讲解",
    )
    assert style.selection in plan_user

    script_user = P.SCRIPT_USER.format(
        style=style.name, style_card=P.style_card(style, "script"),
        sample=P.style_sample(style),
        hooks=P.style_hooks(style), endings=P.style_endings(style),
        pov_line=P.pov_line(style), synopsis="梗概",
        characters="男主（外卖员）", arc="他要在午夜前洗清冤屈",
        beats="s001: 开场", prev="（无）", segments="- id=s001 时长=5.0s", n=1, rate=4.6,
    )
    assert style.diction in script_user
    assert style.sample in script_user
    assert style.hooks[0] in script_user
    assert style.endings[0] in script_user
    assert "男主（外卖员）" in script_user       # 角色表注入（故事性）
    assert "他要在午夜前洗清冤屈" in script_user   # 故事主线注入

    analyze_user = P.ANALYZE_USER.format(
        style=style.name, style_card=P.style_card(style, "analyze"), target=180,
        duration=100, name="影片", timeline="[画面] 讲解",
    )
    assert style.selection in analyze_user

    story_user = P.STORYLINE_USER.format(
        style=style.name, style_card=P.style_card(style, "story"),
        synopsis="梗概", characters="无", segments="- s001", n=1,
    )
    assert style.structure in story_user

    repair_user = P.REPAIR_USER.format(
        style=style.name, style_card=P.style_card(style, "repair"),
        cur=10, caption="文案", target=20, lo=18, hi=22, dur=5.0, transcript="台词",
    )
    assert style.diction in repair_user
