"""ComfyUI 视觉理解工作流（vlm.provider=comfyui）测试。"""

import json
import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from taggycat.config import load_config  # noqa: E402
from taggycat.llm import (  # noqa: E402
    ComfyUIVLMClient,
    LLMApiError,
    VLMClient,
    get_vlm,
)

ROOT = Path(__file__).resolve().parent.parent

# API 格式工作流：LoadImage + 文本提示 + 文本输出
API_WF = {
    "1": {"class_type": "LoadImage",
          "inputs": {"image": "old.png", "upload": "image"}},
    "2": {"class_type": "QwenVL",
          "inputs": {"text": "内置旧提示词", "image": ["1", 0]}},
    "3": {"class_type": "ShowText",
          "inputs": {"text": ["2", 0]}},
}


@pytest.fixture
def vlm_cfg(tmp_path):
    cfg = load_config(ROOT / "config.example.toml")
    wf = tmp_path / "vlm_workflow.json"
    wf.write_text(json.dumps(API_WF), encoding="utf-8")
    cfg.vlm.enabled = True
    cfg.vlm.provider = "comfyui"
    cfg.vlm.comfyui.workflow = str(wf)
    cfg.vlm.comfyui.poll_interval = 0.0
    return cfg


# ---------------------------------------------------------------------
# 配置与工厂
# ---------------------------------------------------------------------
def test_example_config_has_vlm_comfyui_section():
    cfg = load_config(ROOT / "config.example.toml")
    assert cfg.vlm.provider == "openai_compatible"
    assert cfg.vlm.comfyui.base_url.startswith("http")
    assert cfg.vlm.comfyui.workflow == ""


def test_get_vlm_returns_comfyui_client(vlm_cfg):
    v = get_vlm(vlm_cfg)
    assert isinstance(v, ComfyUIVLMClient)


def test_get_vlm_comfyui_requires_workflow(vlm_cfg):
    vlm_cfg.vlm.comfyui.workflow = ""
    with pytest.raises(LLMApiError, match="vlm.comfyui.workflow"):
        get_vlm(vlm_cfg)


def test_get_vlm_openai_provider_unchanged():
    cfg = load_config(ROOT / "config.example.toml")
    cfg.vlm.provider = "openai_compatible"
    cfg.vlm.api_key = "sk-x"
    cfg.vlm.base_url = "https://x/v1"
    cfg.vlm.model = "qwen-vl-max"
    assert isinstance(get_vlm(cfg), VLMClient)
    cfg.vlm.api_key = ""
    assert get_vlm(cfg) is None


# ---------------------------------------------------------------------
# describe_batch（mock ComfyUI HTTP）
# ---------------------------------------------------------------------
class _Resp:
    def __init__(self, payload, status=200):
        self.status_code = status
        self._payload = payload
        self.text = json.dumps(payload, ensure_ascii=False)

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


def _mock_comfy(monkeypatch, desc_text="昏暗的房间里，一名男子坐在桌前翻阅文件"):
    """mock /upload/image + /prompt + /history，记录调用。"""
    import taggycat.comfy as C

    calls = []

    def fake_post(url, **kw):
        calls.append(("POST", url, kw))
        if url.endswith("/upload/image"):
            return _Resp({"name": "uploaded.png", "subfolder": "", "type": "input"})
        if url.endswith("/prompt"):
            return _Resp({"prompt_id": "pid-1"})
        raise AssertionError(f"意外请求: {url}")

    def fake_get(url, **kw):
        calls.append(("GET", url, kw))
        if "/history/" in url:
            return _Resp({"pid-1": {
                "status": {"status_str": "success"},
                "outputs": {"3": {"ui": {"text": [desc_text]}}},
            }})
        raise AssertionError(f"意外请求: {url}")

    from types import SimpleNamespace

    monkeypatch.setattr(
        C, "get_session",
        lambda: SimpleNamespace(post=fake_post, get=fake_get),
    )
    return calls


def test_describe_batch_end_to_end(vlm_cfg, tmp_path, monkeypatch):
    calls = _mock_comfy(monkeypatch)
    frame = tmp_path / "f1.png"
    frame.write_bytes(b"\x89PNG")

    client = get_vlm(vlm_cfg)
    seen: list[tuple[int, int]] = []
    results = client.describe_batch(
        [(12.5, str(frame))], style_hint="悬疑解说",
        on_progress=lambda d, t: seen.append((d, t)),
    )

    assert results == [{"t": 12.5, "desc": "昏暗的房间里，一名男子坐在桌前翻阅文件"}]
    assert seen == [(1, 1)]               # 每帧完成后回调 (已处理, 总数)
    urls = [u for _, u, _ in calls]
    assert any("/upload/image" in u for u in urls)
    assert any("/prompt" in u for u in urls)

    # 提交的工作流：图像已替换、提示词已注入
    post_prompt = next(kw for m, u, kw in calls
                       if m == "POST" and u.endswith("/prompt"))
    wf = post_prompt["json"]["prompt"]
    assert wf["1"]["inputs"]["image"] == "uploaded.png"
    assert "中文" in wf["2"]["inputs"]["text"]      # 我们的指令覆盖了内置提示词


def test_describe_batch_missing_frame_degrades(vlm_cfg, monkeypatch):
    _mock_comfy(monkeypatch)
    client = get_vlm(vlm_cfg)
    # 帧不存在 → 单帧失败被吞掉，契约返回空列表（阶段降级）
    results = client.describe_batch([(0.0, str(ROOT / "nope.png"))])
    assert results == []


def test_workflow_ui_format_rejected(vlm_cfg):
    vlm_cfg.vlm.comfyui.workflow = str(ROOT / "pyproject.toml")
    client = get_vlm(vlm_cfg)
    with pytest.raises(LLMApiError):
        client.describe_batch([(0.0, "whatever.png")])


def test_no_image_input_raises_per_frame(vlm_cfg, tmp_path, monkeypatch):
    # 工作流没有任何图像输入 → 每帧报错 → 返回 []
    bad = {"9": {"class_type": "TextOnly", "inputs": {"text": "x"}}}
    vlm_cfg.vlm.comfyui.workflow = str(tmp_path / "bad.json")
    (tmp_path / "bad.json").write_text(json.dumps(bad), encoding="utf-8")
    frame = tmp_path / "f.png"
    frame.write_bytes(b"x")
    calls = _mock_comfy(monkeypatch)
    client = get_vlm(vlm_cfg)
    assert client.describe_batch([(0.0, str(frame))]) == []
    # 图像输入检查在提交前 → 未提交 /prompt
    assert not any("/prompt" in u for _, u, _ in calls)


# ---------------------------------------------------------------------
# 设置对话框
# ---------------------------------------------------------------------
@pytest.fixture(scope="module")
def app():
    from PyQt6.QtWidgets import QApplication

    pytest.importorskip("PyQt6.QtWidgets", reason="未安装 UI 依赖 (extra: ui)")
    a = QApplication.instance() or QApplication([])
    yield a


def test_settings_dialog_vlm_provider_switch(app, tmp_path):
    from taggycat.ui.settings_dialog import SettingsDialog

    p = tmp_path / "config.toml"
    p.write_bytes((ROOT / "config.example.toml").read_bytes())
    cfg = load_config(p)
    dlg = SettingsDialog(cfg)

    assert dlg.cb_vlm_provider.currentText() == "OpenAI 兼容 API"
    assert dlg.vlm_stack.currentIndex() == 0
    dlg.cb_vlm_provider.setCurrentText("ComfyUI 工作流")
    assert dlg.vlm_stack.currentIndex() == 1   # 自动切页

    dlg.ed_vlm_comfy_url.setText("http://192.168.2.2:8188")
    dlg.ed_vlm_comfy_workflow.setText("resources/workflows/vlm.json")
    dlg._save()

    reloaded = load_config(p)
    assert reloaded.vlm.provider == "comfyui"
    assert reloaded.vlm.comfyui.base_url == "http://192.168.2.2:8188"
    assert reloaded.vlm.comfyui.workflow == "resources/workflows/vlm.json"


# ---------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------
def test_doctor_vlm_comfyui_missing_workflow(tmp_path):
    from taggycat.doctor import FAIL, run_checks

    cfg = load_config(ROOT / "config.example.toml")
    cfg.vlm.provider = "comfyui"
    cfg.vlm.comfyui.workflow = ""
    checks = {c.name: c for c in run_checks(cfg, config_path=None)}
    assert checks["vlm"].level == FAIL
    assert "workflow" in checks["vlm"].message


def test_doctor_vlm_comfyui_workflow_ok_unreachable(tmp_path):
    from taggycat.doctor import WARN, run_checks

    cfg = load_config(ROOT / "config.example.toml")
    wf = tmp_path / "v.json"
    wf.write_text(json.dumps(API_WF), encoding="utf-8")
    cfg.vlm.provider = "comfyui"
    cfg.vlm.comfyui.workflow = str(wf)
    cfg.vlm.comfyui.base_url = "http://127.0.0.1:9"   # 不可达端口
    checks = {c.name: c for c in run_checks(cfg, config_path=None)}
    assert checks["vlm"].level == WARN
    assert "不可达" in checks["vlm"].message
