"""LLM 客户端健壮性修复：base_url 补全 /v1、异常响应解析、VLM 提示词定位。"""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from taggycat.config import load_config
from taggycat.llm import (
    ComfyUIVLMClient,
    LLMApiError,
    LLMClient,
    normalize_openai_base,
)

ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------
# base_url 归一化
# ---------------------------------------------------------------------
@pytest.mark.parametrize(
    ("raw", "expect"),
    [
        ("http://192.168.2.2:1234", "http://192.168.2.2:1234/v1"),   # LM Studio 裸地址
        ("http://127.0.0.1:11434", "http://127.0.0.1:11434/v1"),     # Ollama 裸地址
        ("http://127.0.0.1:11434/v1", "http://127.0.0.1:11434/v1"),   # 已带 /v1
        ("https://api.deepseek.com/v1", "https://api.deepseek.com/v1"),
        ("https://dashscope.aliyuncs.com/compatible-mode/v1",
         "https://dashscope.aliyuncs.com/compatible-mode/v1"),
        ("http://x:1/v1/", "http://x:1/v1"),
        ("http://x:1/chat/completions", "http://x:1/chat/completions"),
        ("", ""),
    ],
)
def test_normalize_openai_base(raw, expect):
    assert normalize_openai_base(raw) == expect


def test_make_client_uses_normalized_base(monkeypatch):
    captured = {}

    class _FakeOpenAI:
        def __init__(self, **kw):
            captured.update(kw)

    import openai

    monkeypatch.setattr(openai, "OpenAI", _FakeOpenAI)
    cfg = load_config(ROOT / "config.example.toml")
    cfg.llm_local.base_url = "http://192.168.2.2:1234"
    LLMClient(cfg.llm_local)
    assert captured["base_url"] == "http://192.168.2.2:1234/v1"


# ---------------------------------------------------------------------
# chat()：choices=null / 分段 content
# ---------------------------------------------------------------------
def _fake_client(resp):
    return SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kw: resp))
    )


def _client_with(resp):
    cfg = load_config(ROOT / "config.example.toml")
    cfg.llm_local.max_retries = 1
    c = LLMClient(cfg.llm_local)
    c._client = _fake_client(resp)
    return c


def test_chat_choices_null_raises_clear_error():
    resp = SimpleNamespace(choices=None, model_dump=lambda: {"choices": None})
    c = _client_with(resp)
    with pytest.raises(LLMApiError) as ei:
        c.chat([{"role": "user", "content": "hi"}])
    msg = str(ei.value)
    assert "TypeError" not in msg          # 不再是裸 NoneType 下标错误
    assert "空内容" in msg or "失败" in msg


def test_chat_content_list_joined():
    parts = [{"type": "text", "text": "你好"}, {"type": "text", "text": "世界"}]
    msg = SimpleNamespace(content=parts)
    resp = SimpleNamespace(choices=[SimpleNamespace(message=msg)],
                           model_dump=lambda: {})
    c = _client_with(resp)
    assert c.chat([{"role": "user", "content": "hi"}]) == "你好世界"


def test_chat_normal_content():
    msg = SimpleNamespace(content="ok")
    resp = SimpleNamespace(choices=[SimpleNamespace(message=msg)],
                           model_dump=lambda: {})
    c = _client_with(resp)
    assert c.chat([{"role": "user", "content": "hi"}]) == "ok"


def test_chat_expands_max_tokens_on_length_truncation():
    """推理模型思维链吃掉配额 → finish_reason=length 时自动扩容重试（不计次）。"""
    calls: list[int | None] = []

    def create(**kw):
        calls.append(kw.get("max_tokens"))
        if len(calls) == 1:
            m = SimpleNamespace(content="", finish_reason="length")
            return SimpleNamespace(
                choices=[SimpleNamespace(message=m, finish_reason="length")],
                model_dump=lambda: {},
            )
        m = SimpleNamespace(content='{"ok": true}', finish_reason="stop")
        return SimpleNamespace(
            choices=[SimpleNamespace(message=m, finish_reason="stop")],
            model_dump=lambda: {},
        )

    c = _client_with(SimpleNamespace(choices=[]))
    c._client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )
    out = c.chat([{"role": "user", "content": "x"}], max_tokens=100)
    assert out == '{"ok": true}'
    assert calls == [100, 200]            # 空内容 + length → 扩容一倍


# ---------------------------------------------------------------------
# reasoning_effort（限制推理模型生成长度）
# ---------------------------------------------------------------------
def test_example_config_has_reasoning_effort():
    cfg = load_config(ROOT / "config.example.toml")
    assert cfg.llm.reasoning_effort == ""
    assert cfg.llm_local.reasoning_effort == ""


def test_chat_sends_reasoning_effort():
    cfg = load_config(ROOT / "config.example.toml")
    cfg.llm_local.max_retries = 1
    cfg.llm_local.reasoning_effort = "none"
    seen: dict = {}

    def create(**kw):
        seen.update(kw)
        m = SimpleNamespace(content="ok", finish_reason="stop")
        return SimpleNamespace(
            choices=[SimpleNamespace(message=m, finish_reason="stop")],
            model_dump=lambda: {},
        )

    c = LLMClient(cfg.llm_local)
    c._client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )
    assert c.chat([{"role": "user", "content": "x"}]) == "ok"
    assert seen["reasoning_effort"] == "none"


def test_chat_drops_unsupported_reasoning_effort():
    """服务端不认识该参数 → 去掉后重试，且不消耗重试次数（max_retries=1 仍成功）。"""
    cfg = load_config(ROOT / "config.example.toml")
    cfg.llm_local.max_retries = 1
    cfg.llm_local.reasoning_effort = "none"
    seen: list[dict] = []

    def create(**kw):
        seen.append(dict(kw))
        if "reasoning_effort" in kw:
            import openai

            raise openai.BadRequestError(
                "Unknown parameter: 'reasoning_effort'",
                response=SimpleNamespace(status_code=400, headers={}, request=None),
                body=None,
            )
        m = SimpleNamespace(content="ok", finish_reason="stop")
        return SimpleNamespace(
            choices=[SimpleNamespace(message=m, finish_reason="stop")],
            model_dump=lambda: {},
        )

    c = LLMClient(cfg.llm_local)
    c._client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )
    assert c.chat([{"role": "user", "content": "x"}]) == "ok"
    assert len(seen) == 2
    assert "reasoning_effort" in seen[0]
    assert "reasoning_effort" not in seen[1]


def test_chat_retries_transport_error_then_succeeds():
    """传输异常属于重试族：首次失败，重试成功。"""
    import openai

    cfg = load_config(ROOT / "config.example.toml")
    cfg.llm_local.max_retries = 2
    calls: list[int] = []

    def create(**kw):
        calls.append(1)
        if len(calls) == 1:
            raise openai.APIConnectionError(request=SimpleNamespace())
        m = SimpleNamespace(content="ok", finish_reason="stop")
        return SimpleNamespace(
            choices=[SimpleNamespace(message=m, finish_reason="stop")],
            model_dump=lambda: {},
        )

    c = LLMClient(cfg.llm_local)
    c._client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )
    assert c.chat([{"role": "user", "content": "x"}]) == "ok"
    assert len(calls) == 2


def test_chat_generic_4xx_not_retried():
    """确定性 4xx（非参数不支持）不重试，直接转 LLMApiError。"""
    import openai

    cfg = load_config(ROOT / "config.example.toml")
    cfg.llm_local.max_retries = 3
    calls: list[int] = []

    def create(**kw):
        calls.append(1)
        raise openai.BadRequestError(
            "invalid messages: bad request",
            response=SimpleNamespace(status_code=400, headers={}, request=None),
            body=None,
        )

    c = LLMClient(cfg.llm_local)
    c._client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )
    with pytest.raises(LLMApiError, match="请求被拒绝"):
        c.chat([{"role": "user", "content": "x"}])
    assert calls == [1]


# ---------------------------------------------------------------------
# VLM 提示词定位（对齐 analyse_image.json 结构）
# ---------------------------------------------------------------------
# LoadImage → ImageScale → AILab_QwenVL(custom_prompt) → easy showAnything(text 回显)
IMAGE_WF = {
    "5": {"class_type": "LoadImage", "inputs": {"image": "girl.png"}},
    "6": {"class_type": "easy showAnything",
          "inputs": {"text": "回显文本不该被覆盖", "anything": ["7", 0]}},
    "7": {"class_type": "AILab_QwenVL",
          "inputs": {"model_name": "Qwen3-VL-8B-Instruct",
                     "custom_prompt": "", "preset_prompt": "Detailed Description",
                     "image": ["8", 0]}},
    "8": {"class_type": "ImageScale",
          "inputs": {"width": "1080", "image": ["5", 0]}},
}


@pytest.fixture
def vlm_client(tmp_path):
    cfg = load_config(ROOT / "config.example.toml")
    wf = tmp_path / "img.json"
    wf.write_text(json.dumps(IMAGE_WF), encoding="utf-8")
    cfg.vlm.provider = "comfyui"
    cfg.vlm.comfyui.workflow = str(wf)
    cfg.vlm.comfyui.poll_interval = 0.0
    return ComfyUIVLMClient(cfg.vlm, root_dir=tmp_path)


def test_patch_targets_model_prompt_not_display(vlm_client):
    instruction = vlm_client._instruction("")
    out = vlm_client._patch_prompt(dict(json.loads(json.dumps(IMAGE_WF))),
                                   instruction, "frame.png")
    assert out["5"]["inputs"]["image"] == "frame.png"        # 图像自动识别
    assert out["7"]["inputs"]["custom_prompt"] == instruction  # 模型提示词
    assert out["6"]["inputs"]["text"] == "回显文本不该被覆盖"   # 展示节点不动
    assert out["7"]["inputs"]["preset_prompt"] == "Detailed Description"


def test_pick_text_excludes_instruction():
    instruction = "请用一句客观的中文描述这张画面"
    outputs = {"6": {"ui": {"text": [instruction, "昏暗的审讯室里，警官低头记录"]}}}
    picked = ComfyUIVLMClient._pick_text(outputs, exclude=instruction)
    assert picked == "昏暗的审讯室里，警官低头记录"
