"""配置写回 + 接口设置对话框测试。"""

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt6.QtWidgets", reason="未安装 UI 依赖 (extra: ui)")

import tomllib  # noqa: E402

from PyQt6.QtWidgets import QPushButton  # noqa: E402

from taggycat.config import (  # noqa: E402
    format_toml_value,
    load_config,
    set_config_value,
    set_config_values,
)

ROOT = Path(__file__).resolve().parent.parent

SAMPLE = """# 顶部注释
[llm]
provider = "openai_compatible"   # 提供方
base_url = "https://old.example.com/v1"
api_key = "sk-old"
model = "old-model"

# 素材参数
[material]
material_ratio = 30
"""


@pytest.fixture(scope="module")
def app():
    from PyQt6.QtWidgets import QApplication

    a = QApplication.instance() or QApplication([])
    yield a


def _sample(tmp_path: Path) -> Path:
    p = tmp_path / "config.toml"
    p.write_text(SAMPLE, encoding="utf-8")
    return p


# ---------------------------------------------------------------------
# set_config_value
# ---------------------------------------------------------------------
def test_set_existing_key(tmp_path):
    p = _sample(tmp_path)
    set_config_value(p, "llm.base_url", "https://new.example.com/v1")
    txt = p.read_text(encoding="utf-8")
    data = tomllib.loads(txt)
    assert data["llm"]["base_url"] == "https://new.example.com/v1"
    assert data["llm"]["api_key"] == "sk-old"          # 其他键不动
    assert "# 顶部注释" in txt and "# 提供方" in txt     # 注释保留
    assert "# 素材参数" in txt


def test_set_preserves_inline_comment(tmp_path):
    p = _sample(tmp_path)
    set_config_value(p, "llm.provider", "ollama")
    txt = p.read_text(encoding="utf-8")
    line = next(ln for ln in txt.splitlines() if ln.startswith("provider"))
    assert 'provider = "ollama"' in line
    assert "# 提供方" in line                 # 行内注释保留
    assert tomllib.loads(txt)["llm"]["provider"] == "ollama"


def test_set_creates_missing_section(tmp_path):
    p = _sample(tmp_path)
    set_config_value(p, "tts.comfyui_clone.workflow", "wf.json")
    data = tomllib.loads(p.read_text(encoding="utf-8"))
    assert data["tts"]["comfyui_clone"]["workflow"] == "wf.json"


def test_set_missing_key_appends_to_section(tmp_path):
    p = _sample(tmp_path)
    set_config_value(p, "llm.timeout", 99)
    data = tomllib.loads(p.read_text(encoding="utf-8"))
    assert data["llm"]["timeout"] == 99
    assert data["llm"]["model"] == "old-model"


def test_set_string_escapes_backslash_and_quote(tmp_path):
    p = _sample(tmp_path)
    win_path = 'E:\\work\\a"b.json'
    set_config_value(p, "llm.model", win_path)
    data = tomllib.loads(p.read_text(encoding="utf-8"))
    assert data["llm"]["model"] == win_path


def test_set_bool_int_float(tmp_path):
    p = _sample(tmp_path)
    set_config_values(p, [
        ("material.material_ratio", 45),
        ("llm.temperature", 0.2),
    ])
    data = tomllib.loads(p.read_text(encoding="utf-8"))
    assert data["material"]["material_ratio"] == 45
    assert data["llm"]["temperature"] == 0.2


def test_format_toml_value():
    assert format_toml_value(True) == "true"
    assert format_toml_value(False) == "false"
    assert format_toml_value(3) == "3"
    assert format_toml_value('a"b') == '"a\\"b"'
    with pytest.raises(TypeError):
        format_toml_value(["x"])


def test_set_on_example_config_roundtrip(tmp_path):
    """对真实示例配置写回后能被 load_config 正常加载。"""
    p = tmp_path / "config.toml"
    p.write_bytes((ROOT / "config.example.toml").read_bytes())
    set_config_values(p, [
        ("llm.api_key", "sk-new-key"),
        ("vlm.enabled", False),
        ("tts.engine", "comfyui"),
    ])
    cfg = load_config(p)
    assert cfg.llm.api_key == "sk-new-key"
    assert cfg.vlm.enabled is False
    assert cfg.tts.engine == "comfyui"


# ---------------------------------------------------------------------
# SettingsDialog
# ---------------------------------------------------------------------
def _dlg_config(tmp_path: Path):
    p = tmp_path / "config.toml"
    p.write_bytes((ROOT / "config.example.toml").read_bytes())
    return load_config(p)


def test_settings_dialog_construct(app, tmp_path):
    from taggycat.ui.settings_dialog import SettingsDialog

    cfg = _dlg_config(tmp_path)
    dlg = SettingsDialog(cfg)
    assert dlg.path == cfg.config_path
    assert dlg.ed_llm_url.text() == cfg.llm.base_url
    assert dlg.ed_llm_key.echoMode() == dlg.ed_llm_key.EchoMode.Password
    assert dlg.chk_vlm.isChecked() is True
    assert dlg.cb_tts_engine.currentData() == cfg.tts.engine   # 数据存引擎键
    assert dlg.cb_tts_engine.count() == 3             # 三个 TTS 引擎
    assert dlg.cb_tts_voice.currentText() == cfg.tts.default_voice
    assert dlg.cb_tts_voice.isEnabled() is True       # edge 需要音色名
    assert dlg.chk_tts_cache.isChecked() is True      # 默认启用配音缓存
    assert dlg.ed_comfy_url.text() == cfg.tts.comfyui.base_url


def test_settings_dialog_save(app, tmp_path, monkeypatch):
    from PyQt6.QtWidgets import QMessageBox

    from taggycat.ui.settings_dialog import SettingsDialog

    cfg = _dlg_config(tmp_path)
    dlg = SettingsDialog(cfg)
    dlg.ed_llm_url.setText("https://api.test/v1")
    dlg.ed_llm_key.setText("sk-secret")
    dlg.ed_llm_model.setText("test-model")
    dlg.chk_vlm.setChecked(False)
    dlg.sp_vlm_frames.setValue(42)
    dlg.cb_tts_engine.setCurrentIndex(dlg.cb_tts_engine.findData("comfyui"))
    dlg.cb_tts_voice.setCurrentText("zh-CN-YunjianNeural")
    dlg.chk_tts_cache.setChecked(False)
    dlg.ed_comfy_workflow.setText("resources/workflows/my.json")

    saved: list = []
    monkeypatch.setattr(QMessageBox, "critical",
                        staticmethod(lambda *a, **k: saved.append(a)))
    dlg._save()
    assert not saved, "保存不应弹错误框"
    assert dlg.result() == dlg.DialogCode.Accepted

    txt = cfg.config_path.read_text(encoding="utf-8")
    assert "# 虎猫解说 配置文件" in txt        # 头部注释仍在
    reloaded = load_config(cfg.config_path)
    assert reloaded.llm.base_url == "https://api.test/v1"
    assert reloaded.llm.api_key == "sk-secret"
    assert reloaded.llm.model == "test-model"
    assert reloaded.vlm.enabled is False
    assert reloaded.vlm.max_frames == 42
    assert reloaded.tts.engine == "comfyui"
    assert reloaded.tts.default_voice == "zh-CN-YunjianNeural"
    assert reloaded.tts.cache is False
    assert reloaded.tts.comfyui.workflow == "resources/workflows/my.json"


def test_tts_engine_switch_toggles_voice_field(app, tmp_path):
    """切到 ComfyUI 引擎时音色输入置灰并提示由工作流/参考音频决定。"""
    from taggycat.ui.settings_dialog import SettingsDialog

    cfg = _dlg_config(tmp_path)
    dlg = SettingsDialog(cfg)

    dlg.cb_tts_engine.setCurrentIndex(dlg.cb_tts_engine.findData("comfyui_clone"))
    assert dlg.cb_tts_voice.isEnabled() is False
    assert "参考音频" in dlg.cb_tts_voice.placeholderText()
    assert dict(dlg._collect())["tts.engine"] == "comfyui_clone"

    dlg.cb_tts_engine.setCurrentIndex(dlg.cb_tts_engine.findData("edge"))
    assert dlg.cb_tts_voice.isEnabled() is True
    assert dlg.cb_tts_voice.currentText() == cfg.tts.default_voice   # 恢复默认音色


def test_probe_openai_success(monkeypatch):
    from taggycat.ui.settings_dialog import probe_openai

    class Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"data": [{"id": "m1"}, {"id": "m2"}]}

    calls = {}

    def fake_get(url, **kw):
        calls["url"] = url
        calls["kw"] = kw
        return Resp()

    monkeypatch.setattr("taggycat.ui.settings_dialog.requests.get", fake_get)
    msg = probe_openai("http://x/v1/", "sk-test")
    assert "连接成功" in msg and "2 个模型" in msg
    assert calls["url"] == "http://x/v1/models"
    assert calls["kw"]["headers"]["Authorization"] == "Bearer sk-test"


def test_probe_openai_failure(monkeypatch):
    import requests

    from taggycat.ui.settings_dialog import probe_openai

    def fake_get(*a, **k):
        raise requests.ConnectionError("拒绝连接")

    monkeypatch.setattr("taggycat.ui.settings_dialog.requests.get", fake_get)
    with pytest.raises(requests.ConnectionError):
        probe_openai("http://127.0.0.1:9/v1")
    with pytest.raises(ValueError):
        probe_openai("")


def _run_probe_button(app, dlg, monkeypatch, fn) -> tuple[QPushButton, list]:
    """点击探针按钮并等线程结束，返回 (按钮, 弹窗记录)。"""
    from PyQt6.QtWidgets import QMessageBox

    boxes: list[tuple[str, str]] = []
    monkeypatch.setattr(
        QMessageBox, "information",
        staticmethod(lambda *a, **k: boxes.append(("ok", a[2]))),
    )
    monkeypatch.setattr(
        QMessageBox, "warning",
        staticmethod(lambda *a, **k: boxes.append(("fail", a[2]))),
    )

    btn = dlg._probe_btn(fn)
    btn.click()
    app.processEvents()
    assert btn.isEnabled() is False
    assert btn.text() == "测试中…"

    worker = btn.property("worker")
    worker.wait(5000)
    app.processEvents()
    return btn, boxes


def test_probe_runs_off_main_thread(app, tmp_path, monkeypatch):
    """TR-17.1: 探针在非主线程执行；期间按钮置灰，完成后恢复文案并回报成功。"""
    import threading
    import time

    from taggycat.ui.settings_dialog import SettingsDialog

    cfg = _dlg_config(tmp_path)
    dlg = SettingsDialog(cfg)

    main_ident = threading.main_thread().ident
    idents: list[int | None] = []

    def fake_fn():
        idents.append(threading.current_thread().ident)
        time.sleep(0.15)
        return "模型可用"

    btn, boxes = _run_probe_button(app, dlg, monkeypatch, fake_fn)

    assert idents and idents[0] != main_ident
    assert btn.isEnabled() is True
    assert btn.text() == "测试连接"
    assert boxes == [("ok", "模型可用")]


def test_probe_failure_warns_and_restores(app, tmp_path, monkeypatch):
    """TR-17.1: 探针异常在槽内兜底 → warning 弹窗，按钮恢复可用。"""
    from taggycat.ui.settings_dialog import SettingsDialog

    cfg = _dlg_config(tmp_path)
    dlg = SettingsDialog(cfg)

    def boom():
        raise RuntimeError("连不上")

    btn, boxes = _run_probe_button(app, dlg, monkeypatch, boom)

    assert btn.isEnabled() is True
    assert btn.text() == "测试连接"
    assert boxes == [("fail", "连不上")]
