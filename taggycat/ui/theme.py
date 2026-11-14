"""虎猫解说 多主题界面（参数化 QSS + Fusion 基底）。

内置 5 套主题，菜单「设置 → 主题」实时切换并写回 config.toml 的 [ui].theme：
  light        浅色·蓝（默认，主色 #2F6FED）
  light-green  浅色·青绿
  light-purple 浅色·紫
  dark         深色·蓝黑
  dark-amber   深色·琥珀
"""

from __future__ import annotations

from pathlib import Path
from string import Template
from tempfile import gettempdir

from loguru import logger


def _draw_arrow(path: Path, pointing_down: bool, stroke: str, fill: str) -> None:
    from PyQt6.QtCore import QPoint, QSize
    from PyQt6.QtGui import QColor, QImage, QPainter, QPen, QPolygon

    img = QImage(QSize(12, 8), QImage.Format.Format_ARGB32)
    img.fill(0)
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(QPen(QColor(stroke), 1))
    p.setBrush(QColor(fill))
    pts = (
        [QPoint(1, 1), QPoint(11, 1), QPoint(6, 6)]
        if pointing_down
        else [QPoint(1, 7), QPoint(11, 7), QPoint(6, 2)]
    )
    p.drawPolygon(QPolygon(pts))
    p.end()
    img.save(str(path))


def _ensure_icons(key: str, stroke: str, fill: str) -> str:
    """按主题生成下拉/微调箭头 PNG（Qt QSS 边框三角 hack 不生效，需真图片）。"""
    d = Path(gettempdir()) / "taggycat_icons" / key
    d.mkdir(parents=True, exist_ok=True)
    down, up = d / "arrow_down.png", d / "arrow_up.png"
    if not down.exists():
        _draw_arrow(down, True, stroke, fill)
    if not up.exists():
        _draw_arrow(up, False, stroke, fill)
    return str(d).replace("\\", "/")


# ----------------------------------------------------------------------
# 调色板：浅色基底 + 强调色变体；深色两套独立定义
# ----------------------------------------------------------------------
_LIGHT: dict[str, str] = {
    "display": "浅色·蓝",
    "dark": "0",
    "BG": "#F3F5F9",
    "SURFACE": "#FFFFFF",
    "TEXT": "#1B1F27",
    "TEXT2": "#4B5563",
    "TEXT3": "#A8AFBA",
    "TEXT_MUTED": "#5F6B7A",
    "HINT": "#6B7280",
    "PANEL_TITLE": "#111827",
    "ITEM_TEXT": "#374151",
    "BORDER": "#E4E7ED",
    "BORDER_IN": "#D5DAE2",
    "BORDER_LIGHT": "#EEF1F5",
    "SEL": "#E8F0FE",
    "SEL_FG": "#1D4ED8",
    "ACCENT": "#2F6FED",
    "ACCENT_H": "#1F5FE0",
    "ACCENT_P": "#1A54C8",
    "ACCENT_D": "#AFC7F5",
    "ACCENT2": "#5B8DF6",
    "HOVER_BG": "#F5F8FF",
    "HOVER_BR": "#A9C2F8",
    "DISABLED_BG": "#F2F3F5",
    "GHOST_HOVER": "#EDF1F7",
    "ROW_ALT": "#FAFBFD",
    "HEADER_BG": "#F7F8FA",
    "HEADER_H": "#EFF2F7",
    "PROGRESS_BG": "#EDF0F5",
    "SLIDER_GROOVE": "#E3E7EE",
    "CHECK_BR": "#C3C9D2",
    "SCROLL": "#C9CFD9",
    "SCROLL_H": "#A8B1BF",
    "LOG_BG": "#14181F",
    "LOG_FG": "#C6CEDA",
    "LOG_BR": "#0B0E13",
    "STATUS_ACTIVE": "#2563EB",
    "STATUS_WAIT": "#D97706",
    "STATUS_QUEUE": "#6B7280",
    "TOOLTIP_BG": "#1B1F27",
    "TOOLTIP_FG": "#FFFFFF",
}

_LIGHT_GREEN: dict[str, str] = {
    **_LIGHT,
    "display": "浅色·青绿",
    "SEL": "#DFF5F2",
    "SEL_FG": "#0B756B",
    "ACCENT": "#0F9B8E",
    "ACCENT_H": "#0B857A",
    "ACCENT_P": "#0A746A",
    "ACCENT_D": "#9BD8D1",
    "ACCENT2": "#2BB8A9",
    "HOVER_BG": "#F0FBFA",
    "HOVER_BR": "#7ED0C8",
    "STATUS_ACTIVE": "#0F9B8E",
}

_LIGHT_PURPLE: dict[str, str] = {
    **_LIGHT,
    "display": "浅色·紫",
    "SEL": "#EFE9FD",
    "SEL_FG": "#6D28D9",
    "ACCENT": "#7C3AED",
    "ACCENT_H": "#6D28D9",
    "ACCENT_P": "#5B21B6",
    "ACCENT_D": "#C9B6F8",
    "ACCENT2": "#9F7AEA",
    "HOVER_BG": "#F7F4FE",
    "HOVER_BR": "#C4B1F5",
    "STATUS_ACTIVE": "#7C3AED",
}

_DARK: dict[str, str] = {
    "display": "深色·蓝黑",
    "dark": "1",
    "BG": "#12151C",
    "SURFACE": "#1A1F29",
    "TEXT": "#DDE3EC",
    "TEXT2": "#98A2B3",
    "TEXT3": "#5C6577",
    "TEXT_MUTED": "#8B95A7",
    "HINT": "#7C8698",
    "PANEL_TITLE": "#F0F3F8",
    "ITEM_TEXT": "#C5CDDA",
    "BORDER": "#2A3242",
    "BORDER_IN": "#384153",
    "BORDER_LIGHT": "#232A38",
    "SEL": "#24344F",
    "SEL_FG": "#82B1FF",
    "ACCENT": "#4C8DFF",
    "ACCENT_H": "#3A7CF0",
    "ACCENT_P": "#2F6DD6",
    "ACCENT_D": "#2E4470",
    "ACCENT2": "#6FA8FF",
    "HOVER_BG": "#222A3A",
    "HOVER_BR": "#40598A",
    "DISABLED_BG": "#202633",
    "GHOST_HOVER": "#222A3A",
    "ROW_ALT": "#171C26",
    "HEADER_BG": "#1E2430",
    "HEADER_H": "#262E3E",
    "PROGRESS_BG": "#232A38",
    "SLIDER_GROOVE": "#2E3648",
    "CHECK_BR": "#4A5468",
    "SCROLL": "#3A4356",
    "SCROLL_H": "#4E586E",
    "LOG_BG": "#0E1117",
    "LOG_FG": "#B9C2D0",
    "LOG_BR": "#080A0E",
    "STATUS_ACTIVE": "#6FA8FF",
    "STATUS_WAIT": "#E8A33D",
    "STATUS_QUEUE": "#98A2B3",
    "TOOLTIP_BG": "#262C3A",
    "TOOLTIP_FG": "#E6EAF2",
}

_DARK_AMBER: dict[str, str] = {
    **_DARK,
    "display": "深色·琥珀",
    "BG": "#17140E",
    "SURFACE": "#201B13",
    "TEXT": "#EEE5D6",
    "TEXT2": "#A99C86",
    "TEXT3": "#6C6353",
    "TEXT_MUTED": "#9A8F7C",
    "HINT": "#8E8371",
    "PANEL_TITLE": "#F5EDE0",
    "ITEM_TEXT": "#D6CBB8",
    "BORDER": "#33291C",
    "BORDER_IN": "#423626",
    "BORDER_LIGHT": "#2A2218",
    "SEL": "#3C2F1A",
    "SEL_FG": "#F0B94E",
    "ACCENT": "#F59E0B",
    "ACCENT_H": "#DE8B06",
    "ACCENT_P": "#C57C05",
    "ACCENT_D": "#5C4318",
    "ACCENT2": "#FBBF24",
    "HOVER_BG": "#2A2317",
    "HOVER_BR": "#7A5E28",
    "DISABLED_BG": "#262015",
    "GHOST_HOVER": "#2A2317",
    "ROW_ALT": "#1A160F",
    "HEADER_BG": "#241E15",
    "HEADER_H": "#2C2519",
    "PROGRESS_BG": "#2A2317",
    "SLIDER_GROOVE": "#382F20",
    "CHECK_BR": "#574934",
    "SCROLL": "#4A4030",
    "SCROLL_H": "#5E5340",
    "LOG_BG": "#100D08",
    "LOG_FG": "#C4B9A4",
    "LOG_BR": "#0A0805",
    "STATUS_ACTIVE": "#FBBF24",
    "STATUS_QUEUE": "#A99C86",
    "TOOLTIP_BG": "#2C2519",
    "TOOLTIP_FG": "#EEE5D6",
}

_THEMES: dict[str, dict[str, str]] = {
    "light": _LIGHT,
    "light-green": _LIGHT_GREEN,
    "light-purple": _LIGHT_PURPLE,
    "dark": _DARK,
    "dark-amber": _DARK_AMBER,
}


def theme_names() -> list[str]:
    """全部主题 key（菜单顺序）。"""
    return list(_THEMES)


def theme_display(key: str) -> str:
    """主题显示名（未知 key 原样返回）。"""
    return _THEMES.get(key, {}).get("display", key)


def theme_items() -> list[tuple[str, str]]:
    """(key, 显示名) 列表。"""
    return [(k, v["display"]) for k, v in _THEMES.items()]


_TEMPLATE = """
/* ================= 全局 ================= */
* {
    font-family: "Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI", sans-serif;
    font-size: 13px;
    color: $TEXT;
}
QMainWindow, QDialog { background: $BG; }

/* ================= 菜单 ================= */
QMenuBar {
    background: $SURFACE;
    border-bottom: 1px solid $BORDER;
    padding: 2px 8px;
}
QMenuBar::item { padding: 6px 12px; border-radius: 6px; background: transparent; }
QMenuBar::item:selected { background: $SEL; color: $SEL_FG; }
QMenu {
    background: $SURFACE;
    border: 1px solid $BORDER;
    border-radius: 8px;
    padding: 6px;
}
QMenu::item {
    padding: 7px 28px 7px 16px;
    border-radius: 6px;
    margin: 1px 3px;
}
QMenu::item:selected { background: $SEL; color: $SEL_FG; }
QMenu::item:disabled { color: $TEXT3; }
QMenu::separator { height: 1px; background: $BORDER_LIGHT; margin: 5px 8px; }

/* ================= 按钮 ================= */
QPushButton {
    background: $SURFACE;
    border: 1px solid $BORDER_IN;
    border-radius: 7px;
    padding: 6px 14px;
    min-height: 20px;
}
QPushButton:hover { background: $HOVER_BG; border-color: $HOVER_BR; }
QPushButton:pressed { background: $SEL; }
QPushButton:disabled {
    background: $DISABLED_BG;
    color: $TEXT3;
    border-color: $BORDER;
}
QPushButton[class="primary"] {
    background: $ACCENT;
    color: #FFFFFF;
    border: 1px solid $ACCENT;
    font-weight: 600;
    padding: 7px 18px;
}
QPushButton[class="primary"]:hover { background: $ACCENT_H; border-color: $ACCENT_H; }
QPushButton[class="primary"]:pressed { background: $ACCENT_P; border-color: $ACCENT_P; }
QPushButton[class="primary"]:disabled {
    background: $ACCENT_D;
    border-color: $ACCENT_D;
    color: #FFFFFF;
}
QPushButton[class="ghost"] {
    background: transparent;
    border: 1px solid transparent;
    color: $TEXT_MUTED;
    padding: 6px 10px;
}
QPushButton[class="ghost"]:hover { background: $GHOST_HOVER; color: $TEXT; }
QPushButton[class="ghost"]:disabled { background: transparent; color: $TEXT3; }

/* ================= 输入控件 ================= */
QLineEdit, QSpinBox, QDoubleSpinBox {
    background: $SURFACE;
    border: 1px solid $BORDER_IN;
    border-radius: 7px;
    padding: 4px 8px;
    min-height: 18px;
    selection-background-color: $ACCENT;
    selection-color: #FFFFFF;
}
QLineEdit:hover, QSpinBox:hover { border-color: $HOVER_BR; }
QLineEdit:focus, QSpinBox:focus { border: 1px solid $ACCENT; }
QLineEdit:disabled, QSpinBox:disabled { background: $DISABLED_BG; color: $TEXT3; }

QSpinBox::up-button, QSpinBox::down-button {
    background: transparent;
    border: none;
    width: 20px;
}
QSpinBox::up-arrow {
    image: url("$ICON_DIR/arrow_up.png");
    width: 12px;
    height: 8px;
}
QSpinBox::down-arrow {
    image: url("$ICON_DIR/arrow_down.png");
    width: 12px;
    height: 8px;
}

QComboBox {
    background: $SURFACE;
    border: 1px solid $BORDER_IN;
    border-radius: 7px;
    padding: 4px 8px;
    min-height: 18px;
}
QComboBox:hover { border-color: $HOVER_BR; }
QComboBox:focus { border: 1px solid $ACCENT; }
QComboBox::drop-down {
    subcontrol-origin: padding;
    subcontrol-position: center right;
    width: 26px;
    border: none;
    background: transparent;
}
QComboBox::down-arrow {
    image: url("$ICON_DIR/arrow_down.png");
    width: 12px;
    height: 8px;
    margin-right: 7px;
}
QComboBox QAbstractItemView {
    background: $SURFACE;
    border: 1px solid $BORDER;
    border-radius: 8px;
    padding: 5px;
    outline: 0;
    selection-background-color: $SEL;
    selection-color: $SEL_FG;
}

QCheckBox { spacing: 7px; }
QCheckBox::indicator {
    width: 17px;
    height: 17px;
    border: 1px solid $CHECK_BR;
    border-radius: 5px;
    background: $SURFACE;
}
QCheckBox::indicator:hover { border-color: $ACCENT; }
QCheckBox::indicator:checked {
    background: $ACCENT;
    border: 1px solid $ACCENT;
    image: none;
}

/* ================= 滑块 ================= */
QSlider::groove:horizontal {
    height: 6px;
    background: $SLIDER_GROOVE;
    border-radius: 3px;
}
QSlider::sub-page:horizontal { background: $ACCENT; border-radius: 3px; }
QSlider::handle:horizontal {
    width: 16px;
    height: 16px;
    margin: -5px 0;
    border-radius: 8px;
    background: $SURFACE;
    border: 2px solid $ACCENT;
}
QSlider::handle:horizontal:hover { background: $SEL; }

/* ================= 分组卡片 ================= */
QGroupBox {
    background: $SURFACE;
    border: 1px solid $BORDER;
    border-radius: 10px;
    margin-top: 14px;
    padding: 4px 10px 6px 10px;
    font-weight: 600;
}
QGroupBox::title {
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 12px;
    top: 2px;
    padding: 0 6px;
    color: $TEXT2;
}

/* ================= 标签页 ================= */
QTabWidget::pane {
    border: 1px solid $BORDER;
    border-radius: 10px;
    background: $SURFACE;
    top: -1px;
    padding: 6px;
}
QTabBar::tab {
    background: transparent;
    color: $TEXT_MUTED;
    padding: 8px 20px;
    margin-right: 4px;
    border-top-left-radius: 8px;
    border-top-right-radius: 8px;
    font-weight: 600;
}
QTabBar::tab:selected { background: $ACCENT; color: #FFFFFF; }
QTabBar::tab:hover:!selected { background: $SEL; color: $SEL_FG; }

/* ================= 列表 ================= */
QListWidget {
    background: $SURFACE;
    border: 1px solid $BORDER;
    border-radius: 10px;
    padding: 5px;
    outline: 0;
}
QListWidget::item {
    padding: 7px 8px;
    border-radius: 6px;
    color: $ITEM_TEXT;
}
QListWidget::item:hover { background: $ROW_ALT; }
QListWidget::item:selected { background: $SEL; color: $SEL_FG; }

/* ================= 表格 ================= */
QTableWidget {
    background: $SURFACE;
    alternate-background-color: $ROW_ALT;
    border: 1px solid $BORDER;
    border-radius: 8px;
    gridline-color: $BORDER_LIGHT;
    selection-background-color: $SEL;
    selection-color: $TEXT;
    outline: 0;
}
QTableWidget::item { padding: 6px 8px; border: none; }
QHeaderView::section {
    background: $HEADER_BG;
    color: $TEXT_MUTED;
    font-weight: 600;
    font-size: 12px;
    padding: 7px 8px;
    border: none;
    border-bottom: 1px solid $BORDER;
    border-right: 1px solid $BORDER_LIGHT;
}
QHeaderView::section:hover { background: $HEADER_H; }
QTableCornerButton::section { background: $HEADER_BG; border: none; }

/* ================= 进度条 ================= */
QProgressBar {
    background: $PROGRESS_BG;
    border: 1px solid $BORDER;
    border-radius: 7px;
    text-align: center;
    color: $TEXT_MUTED;
    font-size: 12px;
    min-height: 17px;
}
QProgressBar::chunk {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                                stop:0 $ACCENT, stop:1 $ACCENT2);
    border-radius: 6px;
}

/* ================= 日志（终端风，各主题统一深色） ================= */
QPlainTextEdit#logView {
    background: $LOG_BG;
    color: $LOG_FG;
    border: 1px solid $LOG_BR;
    border-radius: 10px;
    padding: 8px;
    font-family: "Consolas", "Cascadia Mono", monospace;
    font-size: 12px;
    selection-background-color: $ACCENT;
    selection-color: #FFFFFF;
}

/* ================= 标签辅助 ================= */
QLabel[role="panelTitle"] { font-size: 15px; font-weight: 700; color: $PANEL_TITLE; }
QLabel[hint="true"] { color: $HINT; font-size: 12px; }

/* ================= 状态栏 ================= */
QStatusBar { background: $SURFACE; border-top: 1px solid $BORDER; }
QStatusBar::item { border: none; }
QLabel#statusActive { color: $STATUS_ACTIVE; padding: 0 12px; font-weight: 600; }
QLabel#statusWaiting { color: $STATUS_WAIT; padding: 0 12px; font-weight: 600; }
QLabel#statusQueue { color: $STATUS_QUEUE; padding: 0 12px; font-weight: 600; }
QLabel#statusLog   { color: $TEXT3; padding: 0 8px; font-size: 12px; }

/* ================= 滚动条 ================= */
QScrollBar:vertical { background: transparent; width: 11px; margin: 3px; }
QScrollBar::handle:vertical {
    background: $SCROLL;
    border-radius: 5px;
    min-height: 32px;
}
QScrollBar::handle:vertical:hover { background: $SCROLL_H; }
QScrollBar:horizontal { background: transparent; height: 11px; margin: 3px; }
QScrollBar::handle:horizontal {
    background: $SCROLL;
    border-radius: 5px;
    min-width: 32px;
}
QScrollBar::handle:horizontal:hover { background: $SCROLL_H; }
QScrollBar::add-line, QScrollBar::sub-line { height: 0; width: 0; }
QScrollBar::add-page, QScrollBar::sub-page { background: transparent; }
QScrollBar::corner { background: transparent; }

/* ================= 提示气泡 ================= */
QToolTip {
    background: $TOOLTIP_BG;
    color: $TOOLTIP_FG;
    border: none;
    padding: 5px 9px;
    border-radius: 5px;
}
"""


def build_stylesheet(key: str) -> str:
    """按主题渲染完整 QSS（未知 key 回退 light）。"""
    p = _THEMES.get(key, _LIGHT)
    icons = _ensure_icons(key if key in _THEMES else "light", p["TEXT_MUTED"], p["HINT"])
    return Template(_TEMPLATE).substitute({**p, "ICON_DIR": icons})


def _apply_palette(app, p: dict[str, str]) -> None:
    """同步原生 QPalette（文件对话框/原生控件跟随主题）。"""
    from PyQt6.QtGui import QColor, QPalette

    pal = QPalette()
    pal.setColor(QPalette.ColorRole.Window, QColor(p["BG"]))
    pal.setColor(QPalette.ColorRole.WindowText, QColor(p["TEXT"]))
    pal.setColor(QPalette.ColorRole.Base, QColor(p["SURFACE"]))
    pal.setColor(QPalette.ColorRole.AlternateBase, QColor(p["ROW_ALT"]))
    pal.setColor(QPalette.ColorRole.Text, QColor(p["TEXT"]))
    pal.setColor(QPalette.ColorRole.Button, QColor(p["SURFACE"]))
    pal.setColor(QPalette.ColorRole.ButtonText, QColor(p["TEXT"]))
    pal.setColor(QPalette.ColorRole.BrightText, QColor("#FFFFFF"))
    pal.setColor(QPalette.ColorRole.Highlight, QColor(p["ACCENT"]))
    pal.setColor(QPalette.ColorRole.HighlightedText, QColor("#FFFFFF"))
    pal.setColor(QPalette.ColorRole.ToolTipBase, QColor(p["TOOLTIP_BG"]))
    pal.setColor(QPalette.ColorRole.ToolTipText, QColor(p["TOOLTIP_FG"]))
    pal.setColor(QPalette.ColorRole.PlaceholderText, QColor(p["TEXT3"]))
    pal.setColor(QPalette.ColorRole.Link, QColor(p["ACCENT"]))
    app.setPalette(pal)


def apply_theme(app, name: str | None = None) -> None:
    """给 QApplication 应用 Fusion 基底 + 全局 QSS + 原生调色板。"""
    key = name if name in _THEMES else "light"
    if name and name not in _THEMES:
        logger.warning("未知主题 {}，回退 light", name)
    app.setStyle("Fusion")
    app.setStyleSheet(build_stylesheet(key))
    _apply_palette(app, _THEMES[key])
