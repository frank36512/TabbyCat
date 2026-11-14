"""多主题测试：调色板渲染、应用切换、主窗口主题菜单与持久化。"""

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt6.QtWidgets", reason="未安装 UI 依赖 (extra: ui)")

from PyQt6.QtWidgets import QApplication  # noqa: E402

from taggycat.config import load_config  # noqa: E402
from taggycat.ui import theme as th  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def app():
    a = QApplication.instance() or QApplication([])
    yield a


@pytest.fixture
def config(tmp_path):
    cfg = load_config(ROOT / "config.example.toml")
    cfg.paths.workspace = str(tmp_path / "ws")
    cfg.paths.output = str(tmp_path / "out")
    return cfg


def test_theme_catalog():
    names = th.theme_names()
    assert {"light", "light-green", "light-purple", "dark", "dark-amber"} <= set(names)
    assert th.theme_display("light") == "浅色·蓝"
    assert th.theme_display("nope") == "nope"
    assert [k for k, _ in th.theme_items()] == names


def test_build_stylesheet_for_all_themes():
    for key in th.theme_names():
        qss = th.build_stylesheet(key)
        assert "QPushButton" in qss
        assert "url(" in qss
        # 无残留占位符（substitute 缺 token 会直接抛 KeyError）
        assert "$" not in qss


def test_apply_theme_all_and_fallback(app):
    for key in th.theme_names():
        th.apply_theme(app, key)
        assert app.styleSheet() == th.build_stylesheet(key)
    th.apply_theme(app, "nope")  # 未知主题回退 light，不崩溃
    assert app.styleSheet() == th.build_stylesheet("light")
    th.apply_theme(app, "light")  # 还原默认，避免影响其他用例


def test_mainwindow_theme_menu(app, config):
    from taggycat.ui.main_window import MainWindow

    win = MainWindow(config)
    assert set(win.theme_actions) == set(th.theme_names())
    checked = [k for k, a in win.theme_actions.items() if a.isChecked()]
    assert checked == [config.ui.theme]
    win.close()


def test_switch_theme_persists(app, config, monkeypatch):
    from taggycat.ui.main_window import MainWindow

    win = MainWindow(config)
    captured: dict = {}
    monkeypatch.setattr(
        "taggycat.ui.main_window.find_config",
        lambda *a, **k: Path("fake-config.toml"),
    )
    monkeypatch.setattr(
        "taggycat.ui.main_window.set_config_values",
        lambda p, items: captured.update(path=p, items=list(items)),
    )
    win._switch_theme("dark")
    assert captured["items"] == [("ui.theme", "dark")]
    assert win.theme_actions["dark"].isChecked()
    assert not win.theme_actions["light"].isChecked()
    assert app.styleSheet() == th.build_stylesheet("dark")
    th.apply_theme(app, "light")  # 还原默认，避免影响其他用例
    win.close()
