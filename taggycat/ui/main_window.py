"""虎猫解说 主窗口：基本信息/剪辑配置 → 素材池 → 操作按钮 → 任务队列（日志在帮助菜单）。"""

from __future__ import annotations

import subprocess
from pathlib import Path

from loguru import logger
from PyQt6.QtCore import Qt, QTimer, QUrl
from PyQt6.QtGui import (
    QAction,
    QActionGroup,
    QBrush,
    QCloseEvent,
    QColor,
    QIcon,
    QKeySequence,
    QShortcut,
)
from PyQt6.QtMultimedia import QAudioOutput, QMediaPlayer
from PyQt6.QtMultimediaWidgets import QVideoWidget
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSlider,
    QSpinBox,
    QStackedWidget,
    QStatusBar,
    QStyle,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from taggycat import APP_ID, APP_NAME, __version__
from taggycat.config import AppConfig, find_config, load_config, set_config_values
from taggycat.models import STAGE_ORDER, ProjectConfig, StageStatus, derive_task_state
from taggycat.prompts import STYLE_PRESETS, get_style
from taggycat.store import TaskStore
from taggycat.ui.batch_dialog import (
    MODE_PER_DIR,
    MODE_PER_FILE,
    BatchDialog,
    build_plan,
)
from taggycat.ui.log_view import LogBridge, LogViewDialog
from taggycat.ui.review_dialog import ReviewDialog
from taggycat.ui.settings_dialog import (
    EDGE_VOICES,
    SettingsDialog,
    fill_tts_engines,
    sync_voice_field,
)
from taggycat.ui.theme import apply_theme, theme_display, theme_items
from taggycat.ui.workers import DeleteWorker, PipelineWorker

VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".flv", ".wmv", ".ts", ".m4v", ".webm"}

# 叙述视角下拉：显示文本 → ProjectConfig.pov 值
POV_MODES = ["跟随风格", "第三人称", "第一人称（代入角色）"]
_POV_VALUES = {"跟随风格": "", "第三人称": "third", "第一人称（代入角色）": "first"}
REFRESH_MS = 1500

def _resource_root() -> Path:
    """资源根目录：源码运行取仓库根，PyInstaller 打包后取 bundle 根。"""
    import sys
    base = getattr(sys, "_MEIPASS", None)   # PyInstaller onefile 解压目录
    if base:
        return Path(base)
    if getattr(sys, "frozen", False):       # onedir：exe 同级的 _internal
        return Path(sys.executable).resolve().parent / "_internal"
    return Path(__file__).resolve().parent.parent.parent


_ICON_DIR = _resource_root() / "resources" / "icons"


def app_icon() -> QIcon:
    """应用图标（标题栏 / 任务栏 / 状态栏）；资源缺失时返回空图标。"""
    ico = QIcon()
    for size in (16, 24, 32, 48, 64, 128, 256):
        p = _ICON_DIR / f"app_{size}.png"
        if p.is_file():
            ico.addFile(str(p))
    return ico


IDLE_REFRESH_MS = 4000
SEARCH_DEBOUNCE_MS = 200
ROLE_PATH = Qt.ItemDataRole.UserRole
ROLE_ORIGIN = Qt.ItemDataRole(int(Qt.ItemDataRole.UserRole) + 1)  # "file" | "dir"
STATUS_COLORS = {
    "运行中": "#2563EB",
    "已完成": "#16A34A",
    "失败": "#DC2626",
    "待审核": "#D97706",
    "等待中": "#6B7280",
    "已归档": "#A0A7B4",
}


class VideoPreview(QFrame):
    """任务队列右侧的成片预览：选中已完成任务可直接播放。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("previewFrame")

        self._output: Path | None = None
        self._on_open_dir = None

        v = QVBoxLayout(self)
        v.setContentsMargins(8, 8, 8, 8)
        v.setSpacing(6)

        # 文件名标签（加载成片后显示，占位时隐藏）
        self.title = QLabel("")
        self.title.setProperty("hint", True)
        self.title.hide()
        v.addWidget(self.title)

        self.stack = QStackedWidget()
        self.placeholder = QLabel(
            "选择一个已完成的任务\n即可预览生成的视频"
        )
        self.placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.placeholder.setWordWrap(True)
        self.placeholder.setProperty("hint", True)
        self.video = QVideoWidget()
        self.video.setMinimumSize(260, 150)
        self.stack.addWidget(self.placeholder)   # index 0
        self.stack.addWidget(self.video)         # index 1
        v.addWidget(self.stack, 1)

        self.player = QMediaPlayer(self)
        self.audio = QAudioOutput(self)
        self.player.setAudioOutput(self.audio)
        self.player.setVideoOutput(self.video)

        ctrl = QHBoxLayout()
        ctrl.setSpacing(4)
        self.b_play = QPushButton()
        self.b_play.setIcon(
            self.style().standardIcon(QStyle.StandardPixmap.SP_MediaPlay)
        )
        self.b_play.setFixedWidth(36)
        self.b_play.setEnabled(False)
        self.b_play.setToolTip("播放 / 暂停")
        self.b_play.clicked.connect(self.toggle_play)
        ctrl.addWidget(self.b_play)

        self.pos = QSlider(Qt.Orientation.Horizontal)
        self.pos.setEnabled(False)
        ctrl.addWidget(self.pos, 1)

        self.b_open = QPushButton()
        self.b_open.setIcon(
            self.style().standardIcon(QStyle.StandardPixmap.SP_DirOpenIcon)
        )
        self.b_open.setFixedWidth(36)
        self.b_open.setEnabled(False)
        self.b_open.setToolTip("打开输出目录")
        self.b_open.clicked.connect(self.open_output_dir)
        ctrl.addWidget(self.b_open)
        v.addLayout(ctrl)

        self.player.playbackStateChanged.connect(self._on_state_changed)
        self.player.durationChanged.connect(
            lambda d: self.pos.setRange(0, int(d))
        )
        self.player.positionChanged.connect(
            lambda p: self.pos.setValue(int(p))
        )
        self.pos.sliderMoved.connect(self.player.setPosition)

    def load(self, path: Path | str | None, on_open_dir=None) -> None:
        """载入视频文件；不存在/未导出时显示占位；同一路径不重复载入。"""
        if path:
            p = Path(path)
            if p.is_file():
                if self._output == p:      # 已在播放同一文件，避免定时刷新打断
                    self._on_open_dir = on_open_dir
                    return
                self._output = p
                self._on_open_dir = on_open_dir
                self.player.setSource(QUrl.fromLocalFile(str(p)))
                self.stack.setCurrentIndex(1)
                self.b_play.setEnabled(True)
                self.pos.setEnabled(True)
                self.b_open.setEnabled(True)
                self.title.setText(p.name)
                self.title.show()
                return
        self._output = None
        self._on_open_dir = None
        self.player.stop()
        self.stack.setCurrentIndex(0)
        self.b_play.setEnabled(False)
        self.pos.setEnabled(False)
        self.b_open.setEnabled(False)
        self.title.hide()

    def toggle_play(self) -> None:
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.player.pause()
        else:
            self.player.play()

    def open_output_dir(self) -> None:
        if self._output and self._on_open_dir:
            self._on_open_dir(self._output.parent)

    def _on_state_changed(self, state) -> None:
        playing = state == QMediaPlayer.PlaybackState.PlayingState
        pix = (QStyle.StandardPixmap.SP_MediaPause if playing
               else QStyle.StandardPixmap.SP_MediaPlay)
        self.b_play.setIcon(self.style().standardIcon(pix))


class MainWindow(QMainWindow):
    def __init__(self, config: AppConfig):
        super().__init__()
        self.config = config
        self.store = TaskStore(config)
        self.worker: PipelineWorker | None = None
        self.active_task: str | None = None
        self.active_name = ""
        self.auto_queue: list[str] = []       # 批量排队待运行的任务
        self._task_cache: list | None = None  # store.list() 缓存（内存过滤复用）
        # 任意线程（worker/loguru sink）→ 主线程的安全日志通道
        self.log_bridge = LogBridge()
        self.log_bridge.line.connect(self._append_log)

        self.setWindowTitle(f"{APP_NAME} ({APP_ID}) — AI 影视解说 + 自动化剪辑")
        self.setWindowIcon(app_icon())
        self.resize(1400, 880)
        self.setMinimumSize(1150, 720)
        self._build_ui()
        self._refresh_tasks()

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._on_timer_tick)
        self.timer.start(REFRESH_MS)
        self.search_timer = QTimer(self)
        self.search_timer.setSingleShot(True)
        self.search_timer.timeout.connect(self._refresh_tasks)

        # 快捷键：Ctrl+Enter 开始 / F5 强制刷新 / Delete 删除选中任务
        QShortcut(QKeySequence("Ctrl+Return"), self,
                  activated=self.b_start.click)
        QShortcut(QKeySequence("F5"), self,
                  activated=lambda: self._refresh_tasks(force=True))
        QShortcut(QKeySequence("Delete"), self,
                  activated=self._delete_selected)

    # ------------------------------------------------------------------
    # UI 构建
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        mb = self.menuBar()

        m_file = mb.addMenu("文件(&F)")
        a_out = m_file.addAction("打开输出目录(&O)")
        a_out.triggered.connect(lambda: self._open_dir(self.config.output_dir))
        a_cache = m_file.addAction("打开缓存目录(&K)")
        a_cache.triggered.connect(
            lambda: self._open_dir(self.config.resolve(self.config.paths.tts_cache))
        )
        m_file.addSeparator()
        a_quit = m_file.addAction("退出(&Q)")
        a_quit.setShortcut("Ctrl+Q")
        a_quit.triggered.connect(self.close)

        m_set = mb.addMenu("设置(&S)")
        a_cfg = m_set.addAction("接口设置…(&C)")
        a_cfg.setShortcut("Ctrl+,")
        a_cfg.triggered.connect(self._open_settings)
        m_set.addSeparator()
        m_theme = m_set.addMenu("主题(&T)")
        grp = QActionGroup(self)
        grp.setExclusive(True)
        self.theme_actions: dict[str, QAction] = {}
        for key, disp in theme_items():
            act: QAction = grp.addAction(disp)
            act.setCheckable(True)
            act.setChecked(key == self.config.ui.theme)
            act.triggered.connect(lambda _checked=False, k=key: self._switch_theme(k))
            m_theme.addAction(act)
            self.theme_actions[key] = act

        m_help = mb.addMenu("帮助(&H)")
        a_log = m_help.addAction("运行日志(&L)")
        a_log.setShortcut("Ctrl+L")
        a_log.triggered.connect(self._show_log)
        a_about = m_help.addAction(f"关于 {APP_NAME}…")
        a_about.triggered.connect(self._show_about)

        # 日志窗口：帮助菜单打开（隐藏时仍持续缓存输出）
        self.log_dialog = self._build_log_dialog()

        central = QWidget()
        lay = QVBoxLayout(central)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.setSpacing(10)
        # 首行：基本信息 ∥ 节奏与输出（各占一半宽）
        top_row = QHBoxLayout()
        top_row.setSpacing(10)
        top_row.addWidget(self._build_basic_card(), 1)
        top_row.addWidget(self._build_output_card(), 1)
        lay.addLayout(top_row)
        # 中行：素材池 ∥ 任务队列 ∥ 成片预览，此行占据剩余高度
        mid_row = QHBoxLayout()
        mid_row.setSpacing(10)
        mid_row.addWidget(self._build_pool_card(), 1)
        mid_row.addWidget(self._build_queue_card(), 2)
        mid_row.addWidget(self._build_preview_card(), 2)
        lay.addLayout(mid_row, 1)
        # 底部：操作按钮行
        lay.addLayout(self._build_control_row())
        self.setCentralWidget(central)

        sb = QStatusBar()
        self.setStatusBar(sb)
        self.status_active = QLabel("● 活动中 0")
        self.status_active.setObjectName("statusActive")
        self.status_waiting = QLabel("● 等待中 0")
        self.status_waiting.setObjectName("statusWaiting")
        self.status_queue = QLabel("● 队列 0")
        self.status_queue.setObjectName("statusQueue")
        sb.addWidget(self.status_active)
        sb.addWidget(self.status_waiting)
        sb.addWidget(self.status_queue)
        self.status_log = QLabel("")
        self.status_log.setObjectName("statusLog")
        sb.addWidget(self.status_log, 1)     # 占满剩余宽度，实时滚动最新日志
        sb.addPermanentWidget(QLabel("Ctrl+Q 退出 · 双击任务查看详情 · 右键更多操作"))

    def _build_control_row(self) -> QHBoxLayout:
        """操作行（素材池下方）：开始/停止/审核/批量 … 接口设置/打开输出目录。"""
        ctrl = QHBoxLayout()
        ctrl.setSpacing(8)
        self.b_start = QPushButton("▶ 开始任务")
        self.b_start.setProperty("class", "primary")
        self.b_start.clicked.connect(self._start_task)
        self.b_stop = QPushButton("■ 停止")
        self.b_stop.setEnabled(False)
        self.b_stop.clicked.connect(self._stop_task)
        self.b_review = QPushButton("审核文案…")
        self.b_review.setEnabled(False)
        self.b_review.clicked.connect(self._open_review)
        self.b_batch = QPushButton("批量队列…")
        self.b_batch.clicked.connect(self._open_batch)
        for b in (self.b_start, self.b_stop, self.b_review, self.b_batch):
            ctrl.addWidget(b)
        ctrl.addStretch(1)
        self.b_settings = QPushButton("接口设置…")
        self.b_settings.setProperty("class", "ghost")
        self.b_settings.clicked.connect(self._open_settings)
        self.b_open_out = QPushButton("打开输出目录")
        self.b_open_out.setProperty("class", "ghost")
        self.b_open_out.clicked.connect(
            lambda: self._open_dir(self.config.output_dir)
        )
        ctrl.addWidget(self.b_settings)
        ctrl.addWidget(self.b_open_out)
        return ctrl

    def _build_log_dialog(self) -> LogViewDialog:
        """运行日志窗口（帮助 → 运行日志）。"""
        return LogViewDialog(self)

    def _show_log(self) -> None:
        self.log_dialog.show()
        self.log_dialog.raise_()
        self.log_dialog.activateWindow()

    def _show_about(self) -> None:
        QMessageBox.about(
            self,
            f"关于 {APP_NAME}",
            f"<b>{APP_NAME} ({APP_ID})</b> v{__version__}<br>"
            "AI 影视解说 + 自动化剪辑<br><br>"
            "素材 → 文案 → 配音 → 字幕 → 成片，本地一键流水线。<br><br>"
            "开源许可：Apache-2.0",
        )

    def _build_pool_card(self) -> QGroupBox:
        box = QGroupBox("素材池")
        lay = QVBoxLayout(box)
        lay.setSpacing(8)

        head = QHBoxLayout()
        sub = QLabel(
            "可多选文件或整个目录，双击条目移除；两种方式不可混用："
            "多选文件=整池一个任务，目录=按目录分别出片（多目录自动排队）。"
            "添加目录会递归扫描全部子目录。"
        )
        sub.setProperty("hint", True)
        sub.setWordWrap(True)
        head.addWidget(sub, 1)
        self.pool_hint = QLabel("0 个文件")
        self.pool_hint.setProperty("hint", True)
        head.addWidget(self.pool_hint)
        lay.addLayout(head)

        self.pool = QListWidget()
        self.pool.setMinimumHeight(90)   # 高度随「素材池∥任务队列」行拉伸
        self.pool.setWordWrap(True)
        self.pool.itemDoubleClicked.connect(lambda _: self._remove_selected())
        lay.addWidget(self.pool, 1)

        btns = QHBoxLayout()
        b_add = QPushButton("添加视频…")
        b_add.clicked.connect(self._add_files)
        b_add_dir = QPushButton("添加目录…")
        b_add_dir.clicked.connect(self._add_dir)
        b_del = QPushButton("移除")
        b_del.setProperty("class", "ghost")
        b_del.clicked.connect(self._remove_selected)
        btns.addWidget(b_add)
        btns.addWidget(b_add_dir)
        btns.addStretch(1)
        btns.addWidget(b_del)
        lay.addLayout(btns)
        return box

    def _build_basic_card(self) -> QGroupBox:
        g_basic = QGroupBox("基本信息")
        form = QGridLayout(g_basic)
        form.setHorizontalSpacing(10)
        form.setVerticalSpacing(9)
        form.setColumnStretch(1, 1)
        form.setColumnStretch(3, 1)

        self.ed_name = QLineEdit("解说任务")
        form.addWidget(self._form_label("任务名称"), 0, 0)
        form.addWidget(self.ed_name, 0, 1)

        self.cb_lang = QComboBox()
        self.cb_lang.addItems(["中文", "English", "日本語"])
        self.cb_lang.setToolTip("预留：当前版本提示词固定为中文")
        form.addWidget(self._form_label("输出语言"), 0, 2)
        form.addWidget(self.cb_lang, 0, 3)

        self.cb_style = QComboBox()
        self.cb_style.addItems(list(STYLE_PRESETS.keys()))
        form.addWidget(self._form_label("解说风格"), 1, 0)
        form.addWidget(self.cb_style, 1, 1)

        self.cb_tts_engine = QComboBox()
        fill_tts_engines(self.cb_tts_engine, self.config.tts.engine)
        self.cb_tts_engine.setToolTip(
            "配音引擎：Edge 在线语音 / ComfyUI 工作流 TTS / ComfyUI 语音克隆。\n"
            "工作流 JSON 与克隆参考音频在「设置 → 语音配置」中配置。"
        )
        form.addWidget(self._form_label("配音引擎"), 1, 2)
        form.addWidget(self.cb_tts_engine, 1, 3)

        self.cb_voice = QComboBox()
        self.cb_voice.setEditable(True)
        self.cb_voice.addItems(EDGE_VOICES)
        self.cb_voice.setCurrentText(self.config.tts.default_voice)
        form.addWidget(self._form_label("配音音色"), 2, 2)
        form.addWidget(self.cb_voice, 2, 3)
        self.cb_tts_engine.currentIndexChanged.connect(self._sync_voice_engine)
        self._sync_voice_engine()

        self.cb_pov = QComboBox()
        self.cb_pov.addItems(POV_MODES)
        self.cb_pov.setToolTip(
            "叙述人称：第一人称更沉浸，可在右侧填代入的角色名；默认跟随所选风格"
        )
        form.addWidget(self._form_label("叙述视角"), 2, 0)
        form.addWidget(self.cb_pov, 2, 1)

        self.ed_pov_char = QLineEdit()
        self.ed_pov_char.setPlaceholderText("（第一人称时可选）角色名，留空=主角")
        form.addWidget(self._form_label("代入角色"), 3, 0)
        form.addWidget(self.ed_pov_char, 3, 1)

        self.ed_rate = QLineEdit()
        self.ed_rate.setPlaceholderText("（可选）语速覆盖，如 +10%；留空=按风格语速")
        form.addWidget(self._form_label("语速覆盖"), 3, 2)
        form.addWidget(self.ed_rate, 3, 3, 1, 1)

        # 风格要点提示：所选风格的关键特征与推荐音色（随选择实时更新）
        self.lb_style_hint = QLabel()
        self.lb_style_hint.setWordWrap(True)
        self.lb_style_hint.setObjectName("styleHint")
        form.addWidget(self.lb_style_hint, 4, 0, 1, 4)
        self.cb_style.currentTextChanged.connect(self._update_style_hint)
        self._update_style_hint(self.cb_style.currentText())
        return g_basic

    def _sync_voice_engine(self) -> None:
        """切换配音引擎时同步音色输入（仅 edge 需要音色名）。"""
        sync_voice_field(
            self.cb_voice,
            self.cb_tts_engine.currentData() or "",
            self.config.tts.default_voice,
        )

    def _current_tts_engine(self) -> str:
        return str(self.cb_tts_engine.currentData() or "edge")

    def _update_style_hint(self, name: str) -> None:
        """实时展示所选解说风格的关键特征与推荐音色。"""
        preset = get_style(name)
        self.lb_style_hint.setText(
            f"风格要点：{preset.hook or preset.tone}"
            + (f"　语速：约 {preset.rate:g} 字/秒" if preset.rate else "")
            + (f"　推荐音色：{preset.voice}" if preset.voice else "")
        )
        self.cb_style.setToolTip(
            f"{preset.name}\n"
            f"语气人设：{preset.tone}\n"
            f"开场钩子：{preset.hook}\n"
            f"节奏句长：{preset.pacing}\n"
            f"用词口头禅：{preset.diction}\n"
            f"选材偏好：{preset.selection}"
        )

    def _build_output_card(self) -> QGroupBox:
        g_out = QGroupBox("节奏与输出")
        grid2 = QGridLayout(g_out)
        grid2.setHorizontalSpacing(10)
        grid2.setVerticalSpacing(9)
        grid2.setColumnStretch(1, 1)
        grid2.setColumnStretch(3, 1)

        bgm_row = QHBoxLayout()
        bgm_row.setContentsMargins(0, 0, 0, 0)
        self.ed_bgm = QLineEdit()
        self.ed_bgm.setPlaceholderText("（可选）背景音乐文件")
        b_bgm = QPushButton()
        b_bgm.setFixedWidth(34)
        b_bgm.setIcon(
            b_bgm.style().standardIcon(QStyle.StandardPixmap.SP_DialogOpenButton)
        )
        b_bgm.setToolTip("选择背景音乐文件")
        b_bgm.clicked.connect(self._pick_bgm)
        bgm_row.addWidget(self.ed_bgm)
        bgm_row.addWidget(b_bgm)
        grid2.addWidget(self._form_label("背景音乐"), 0, 0)
        grid2.addLayout(bgm_row, 0, 1)

        self.sp_target = QSpinBox()
        self.sp_target.setRange(15, 3600)
        self.sp_target.setSuffix(" 秒")
        self.sp_target.setValue(self.config.material.target_duration)
        spin_row = QHBoxLayout()
        spin_row.setContentsMargins(0, 0, 0, 0)
        spin_row.addWidget(self.sp_target)
        spin_row.addStretch(1)
        grid2.addWidget(self._form_label("目标时长"), 0, 2)
        grid2.addLayout(spin_row, 0, 3)

        out_row = QHBoxLayout()
        out_row.setContentsMargins(0, 0, 0, 0)
        self.ed_out = QLineEdit(self.config.paths.output)
        b_out = QPushButton()
        b_out.setFixedWidth(34)
        b_out.setIcon(
            b_out.style().standardIcon(QStyle.StandardPixmap.SP_DirOpenIcon)
        )
        b_out.setToolTip("选择输出目录")
        b_out.clicked.connect(self._pick_outdir)
        out_row.addWidget(self.ed_out)
        out_row.addWidget(b_out)
        grid2.addWidget(self._form_label("输出目录"), 1, 0)
        grid2.addLayout(out_row, 1, 1, 1, 3)

        self.sl_material = QSlider(Qt.Orientation.Horizontal)
        self.sl_material.setRange(5, 100)
        self.sl_material.setValue(self.config.material.material_ratio)
        self.sl_material.setToolTip(
            "素材过剩时，从源片分层采样多少比例构造候选（成片按目标时长出片；"
            "素材不过剩时该比例不生效）"
        )
        self.lb_material = QLabel(f"{self.sl_material.value()}%")
        self.lb_material.setFixedWidth(40)
        self.sl_material.valueChanged.connect(
            lambda v: self.lb_material.setText(f"{v}%")
        )
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(self.sl_material, 1)
        row.addWidget(self.lb_material)
        grid2.addWidget(self._form_label("素材选取比例"), 2, 0)
        grid2.addLayout(row, 2, 1)

        self.sl_narration = QSlider(Qt.Orientation.Horizontal)
        self.sl_narration.setRange(0, 100)
        self.sl_narration.setValue(self.config.material.narration_ratio)
        self.lb_narration = QLabel(f"{self.sl_narration.value()}%")
        self.lb_narration.setFixedWidth(40)
        self.sl_narration.valueChanged.connect(
            lambda v: self.lb_narration.setText(f"{v}%")
        )
        self.sl_narration.setToolTip("预留：暂未参与分段规划（成片时长由目标时长决定）")
        row2 = QHBoxLayout()
        row2.setContentsMargins(0, 0, 0, 0)
        row2.addWidget(self.sl_narration, 1)
        row2.addWidget(self.lb_narration)
        grid2.addWidget(self._form_label("解说比例"), 2, 2)
        grid2.addLayout(row2, 2, 3)

        self.chk_validate = QCheckBox("校验文案（配音前人工审核）")
        self.chk_validate.setChecked(self.config.review.validate_script)
        grid2.addWidget(self.chk_validate, 3, 0, 1, 4)
        return g_out

    def _build_queue_card(self) -> QGroupBox:
        queue_box = QGroupBox("任务队列")
        qlay = QVBoxLayout(queue_box)
        qlay.setSpacing(8)

        filt = QHBoxLayout()
        self.b_rerun = QPushButton("↻ 再次运行")
        self.b_rerun.setEnabled(False)
        self.b_rerun.setToolTip("失败/卡住的任务：复位失败阶段后从断点继续（已完成阶段跳过）")
        self.b_rerun.clicked.connect(self._rerun_selected)
        filt.addWidget(self.b_rerun)
        self.ed_filter = QLineEdit()
        self.ed_filter.setPlaceholderText("搜索任务名 / ID…")
        self.ed_filter.setClearButtonEnabled(True)
        self.ed_filter.textChanged.connect(self._schedule_search)
        self.cb_status_filter = QComboBox()
        self.cb_status_filter.addItems(
            ["全部", "等待中", "运行中", "待审核", "已完成", "失败", "已归档"]
        )
        self.cb_status_filter.currentTextChanged.connect(self._refresh_tasks)
        filt.addWidget(self.ed_filter, 1)
        filt.addWidget(self.cb_status_filter)
        qlay.addLayout(filt)

        self.task_table = QTableWidget(0, 5)
        self.task_table.setHorizontalHeaderLabels(
            ["任务", "素材", "当前阶段", "状态", "进度"]
        )
        self.task_table.verticalHeader().hide()
        self.task_table.setAlternatingRowColors(True)
        self.task_table.setCornerButtonEnabled(False)
        self.task_table.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        self.task_table.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows
        )
        self.task_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.task_table.itemDoubleClicked.connect(self._on_task_double_click)
        self.task_table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.task_table.customContextMenuRequested.connect(self._on_task_menu)
        self.task_table.itemSelectionChanged.connect(self._on_selection_changed)

        hdr = self.task_table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        hdr.setSectionResizeMode(2, QHeaderView.ResizeMode.Interactive)
        hdr.setSectionResizeMode(3, QHeaderView.ResizeMode.Interactive)
        hdr.setSectionResizeMode(4, QHeaderView.ResizeMode.Fixed)
        for c, wdt in ((1, 64), (2, 150), (3, 96), (4, 72)):
            self.task_table.setColumnWidth(c, wdt)
        qlay.addWidget(self.task_table, 1)

        self.progress = QProgressBar()
        self.progress.setRange(0, len(STAGE_ORDER))
        self.progress.setFormat("待机")
        qlay.addWidget(self.progress)
        return queue_box

    def _build_preview_card(self) -> QGroupBox:
        """独立卡片：选中已完成任务的成片预览。"""
        box = QGroupBox("成片预览")
        v = QVBoxLayout(box)
        v.setContentsMargins(8, 8, 8, 8)
        self.preview = VideoPreview()
        v.addWidget(self.preview)
        return box

    @staticmethod
    def _form_label(text: str) -> QLabel:
        """两列表单的右对齐字段名标签。"""
        lb = QLabel(text)
        lb.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        return lb

    # ------------------------------------------------------------------
    # 素材池
    # ------------------------------------------------------------------
    def _add_files(self) -> None:
        if self._pool_origin() - {"file"}:
            QMessageBox.warning(
                self, "添加视频",
                "素材池由「添加目录」添加，两种方式不可混用。\n"
                "请先移除目录添加的条目（或清空素材池）后再添加视频。",
            )
            return
        files, _ = QFileDialog.getOpenFileNames(
            self, "选择视频", "",
            "视频文件 (*.mp4 *.mkv *.avi *.mov *.flv *.ts *.m4v);;所有文件 (*)"
        )
        self._append_pool(files, origin="file")

    def _add_dir(self) -> None:
        if self._pool_origin() - {"dir"}:
            QMessageBox.warning(
                self, "添加目录",
                "素材池中有单独添加的视频，与目录添加不可混用。\n"
                "请先移除这些视频（或清空素材池）后再添加目录。",
            )
            return
        d = QFileDialog.getExistingDirectory(self, "选择目录")
        if not d:
            return
        files = [
            str(p) for p in sorted(Path(d).rglob("*"))
            if p.is_file() and p.suffix.lower() in VIDEO_EXTS
        ]
        if not files:
            QMessageBox.information(
                self, "提示", "目录（含全部子目录）中没有视频文件"
            )
            return
        self._append_pool(files, origin="dir")

    def _append_pool(self, files: list[str], origin: str = "file") -> None:
        existing = set()
        for i in range(self.pool.count()):
            it = self.pool.item(i)
            existing.add(str(it.data(ROLE_PATH) or it.text()))
        for f in files:
            if f in existing:
                continue
            item = QListWidgetItem(Path(f).name)
            item.setData(ROLE_PATH, f)
            item.setData(ROLE_ORIGIN, origin)
            item.setToolTip(f)
            self.pool.addItem(item)
        self.pool_hint.setText(f"{self.pool.count()} 个文件")

    def _pool_files(self) -> list[str]:
        """素材池完整路径（显示名为文件名，路径存在 ROLE_PATH）。"""
        out = []
        for i in range(self.pool.count()):
            it = self.pool.item(i)
            out.append(str(it.data(ROLE_PATH) or it.text()))
        return out

    def _pool_origin(self) -> set[str]:
        """池内来源集合 {"file"}/{ "dir"}；无标记按 file 处理。"""
        out = set()
        for i in range(self.pool.count()):
            out.add(str(self.pool.item(i).data(ROLE_ORIGIN) or "file"))
        return out

    def _remove_selected(self) -> None:
        for item in self.pool.selectedItems():
            self.pool.takeItem(self.pool.row(item))
        self.pool_hint.setText(f"{self.pool.count()} 个文件")

    def _pick_bgm(self) -> None:
        f, _ = QFileDialog.getOpenFileName(
            self, "选择背景音乐", "", "音频 (*.mp3 *.wav *.m4a *.flac)"
        )
        if f:
            self.ed_bgm.setText(f)

    def _pick_outdir(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "选择输出目录")
        if d:
            self.ed_out.setText(d)

    def _open_dir(self, p: Path) -> None:
        try:
            p.mkdir(parents=True, exist_ok=True)
            subprocess.Popen(["explorer", str(p)])
        except OSError as e:
            QMessageBox.warning(self, "打开目录", f"无法打开目录：{e}")

    def _open_settings(self) -> None:
        try:
            dlg = SettingsDialog(self.config, self)
        except FileNotFoundError as e:
            QMessageBox.critical(self, "接口设置", str(e))
            return
        if dlg.exec():
            self.config = load_config(dlg.path)
            self.store = TaskStore(self.config)
            # 设置里可能改了默认引擎：同步到任务面板，避免面板仍用旧引擎
            idx = self.cb_tts_engine.findData(self.config.tts.engine)
            if idx >= 0:
                self.cb_tts_engine.setCurrentIndex(idx)
            self._sync_voice_engine()
            self._append_log(f"接口配置已保存: {dlg.path}")

    def _switch_theme(self, key: str) -> None:
        """实时切换全局主题，并写回 config.toml 的 [ui].theme。"""
        try:
            apply_theme(QApplication.instance(), key)
        except Exception:  # noqa: BLE001
            logger.exception("应用主题失败: {}", key)
        for k, act in self.theme_actions.items():
            act.setChecked(k == key)
        try:
            path = self.config.config_path or find_config()
            set_config_values(path, [("ui.theme", key)])
            self._append_log(f"主题已切换：{theme_display(key)}")
        except Exception:  # noqa: BLE001
            logger.exception("保存主题设置失败")

    # ------------------------------------------------------------------
    # 任务
    # ------------------------------------------------------------------
    def _collect_project(self) -> ProjectConfig:
        return ProjectConfig(
            language=self.cb_lang.currentText(),
            style=self.cb_style.currentText(),
            voice=self.cb_voice.currentText().strip(),
            tts_engine=self._current_tts_engine(),
            rate=self.ed_rate.text().strip(),
            pov=_POV_VALUES.get(self.cb_pov.currentText(), ""),
            pov_character=self.ed_pov_char.text().strip(),
            tts_cache=self.config.tts.cache,
            bgm=self.ed_bgm.text().strip(),
            material_ratio=self.sl_material.value(),
            narration_ratio=self.sl_narration.value(),
            target_duration=self.sp_target.value(),
            validate_script=self.chk_validate.isChecked(),
            output_dir=self.ed_out.text().strip() or self.config.paths.output,
        )

    def _start_task(self) -> None:
        if self.worker and self.worker.isRunning():
            QMessageBox.information(self, "提示", "已有任务在运行")
            return
        files = self._pool_files()
        if not files:
            QMessageBox.warning(self, "提示", "请先添加素材视频")
            return

        missing = [f for f in files if not Path(f).exists()]
        if missing:
            QMessageBox.warning(self, "提示", "文件不存在:\n" + "\n".join(missing[:5]))
            return

        # 「添加目录」且含多个父目录：自动按目录拆成多任务排队执行
        if self._pool_origin() == {"dir"}:
            name = self.ed_name.text().strip()
            prefix = (name + "_") if name else ""
            plan = build_plan(files, MODE_PER_DIR, prefix)
            if len(plan) >= 2:
                self._start_tasks_split(plan)
                return

        try:
            st = self.store.create(
                files,
                name=self.ed_name.text().strip() or "解说任务",
                project=self._collect_project(),
            )
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "创建失败", str(e))
            return

        self.active_task = st.task_id
        self._task_cache = None
        self._append_log(f"任务已创建: {st.task_id}")
        self._run_task(st.task_id)

    def _start_tasks_split(self, plan: list[tuple[str, list[str]]]) -> None:
        """把多个分组批量建为任务并排队执行（「开始任务」的多目录拆分路径）。"""
        created: list[str] = []
        try:
            for name, members in plan:
                st = self.store.create(
                    members, name=name, project=self._collect_project()
                )
                created.append(st.task_id)
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(
                self, "创建失败",
                f"已创建 {len(created)} 个任务后失败:\n{e}",
            )
            if not created:
                return
        names = [self.store.load(i).name for i in created]
        self._append_log(
            f"按目录拆分为 {len(created)} 个任务: " + ", ".join(names[:6])
            + ("…" if len(names) > 6 else "")
        )
        self.auto_queue.extend(created)
        self._append_log(f"排队执行已启动，共 {len(self.auto_queue)} 个待运行")
        self._task_cache = None
        self._refresh_tasks()
        if not (self.worker and self.worker.isRunning()):
            self._advance_queue()

    def _run_task(self, task_id: str) -> None:
        try:
            self.active_name = self.store.load(task_id).name
        except Exception:  # noqa: BLE001
            self.active_name = task_id
        self.progress.setToolTip("")
        self.progress.setFormat(f"{self.active_name} · %p%")
        self.worker = PipelineWorker(self.config, task_id)
        self.worker.stage_event.connect(self._on_stage_event)
        self.worker.log.connect(self._append_log)
        self.worker.failed.connect(self._on_failed)
        self.worker.review_required.connect(self._on_review_required)
        self.worker.completed.connect(self._on_completed)
        self.b_start.setEnabled(False)
        self.b_stop.setEnabled(True)
        self.b_review.setEnabled(False)
        self.b_batch.setEnabled(False)
        self.worker.start()

    def _stop_task(self) -> None:
        if self.worker:
            self.worker.stop()
            self._append_log("请求停止…（当前阶段结束后生效）")
            self.b_stop.setEnabled(False)
        if self.auto_queue:
            n = len(self.auto_queue)
            self.auto_queue.clear()
            self._append_log(f"队列已中止（清除 {n} 个待运行任务）")
            self._refresh_tasks()

    def _on_stage_event(self, stage: str, event: str) -> None:
        # 阶段行文由 loguru sink 转发（含 ✔/✖ 标记），这里只管进度
        idx = next((i for i, s in enumerate(STAGE_ORDER) if s.value == stage), -1)
        if event == "done" and idx >= 0:
            self.progress.setValue(idx + 1)
        elif event == "start" and idx >= 0:
            self.progress.setValue(idx)
        self._task_cache = None                    # worker 已写盘
        self._refresh_tasks()

    def _on_failed(self, msg: str) -> None:
        self._append_log(f"✖ {msg}")
        self.b_start.setEnabled(True)
        self.b_stop.setEnabled(False)
        self.b_batch.setEnabled(True)
        self.active_task = None
        if msg == "已取消":
            self.progress.setFormat("待机")
        else:
            # 失败时进度停留并显示错误
            self.progress.setFormat("✖")
            self.progress.setToolTip(msg)
        if self.auto_queue:
            n = len(self.auto_queue)
            self.auto_queue.clear()
            self._append_log(f"任务失败，队列中止（清除 {n} 个待运行任务）")
        self._refresh_tasks()
        if msg != "已取消":
            QMessageBox.warning(self, "任务异常", msg)

    def _on_completed(self, task_id: str) -> None:
        self._append_log(f"✔ 任务完成: {task_id}")
        self.progress.setValue(len(STAGE_ORDER))
        self.b_start.setEnabled(True)
        self.b_stop.setEnabled(False)
        self.b_batch.setEnabled(True)
        if self.active_task == task_id:
            self.active_task = None
        self._task_cache = None                    # worker 已写盘
        self._refresh_tasks()
        if self.auto_queue:
            self._advance_queue()
            return
        self.progress.setFormat("待机")
        state = self.store.load(task_id)
        out = state.stage("export").data.get("output")
        if out:
            box = QMessageBox(self)
            box.setWindowTitle("完成")
            box.setText(f"成片已导出:\n{out}")
            b_open = box.addButton(
                "打开输出目录", QMessageBox.ButtonRole.AcceptRole
            )
            b_close = box.addButton(
                "关闭", QMessageBox.ButtonRole.RejectRole
            )
            box.setDefaultButton(b_close)           # 默认聚焦安全项
            box.exec()
            if box.clickedButton() is b_open:
                self._open_dir(Path(out).parent)
        else:
            QMessageBox.information(self, "完成", f"任务完成: {task_id}")

    def _on_review_required(self, task_id: str) -> None:
        self.b_stop.setEnabled(False)
        self.b_review.setEnabled(True)
        self._task_cache = None                    # worker 已写盘
        self._refresh_tasks()
        self._append_log(f"⏸ 等待文案审核: {task_id}")
        # 直接弹出审核框（用信号里的 task_id，避免多任务开错窗）
        self._open_review(task_id)

    def _reset_controls_after_pause(self) -> None:
        """审核暂停/关闭窗口后恢复按钮（worker 已退出，不会再发信号）。"""
        self.b_start.setEnabled(True)
        self.b_stop.setEnabled(False)
        self.b_review.setEnabled(True)
        self.b_batch.setEnabled(True)

    def _open_review(self, task_id: str | None = None) -> None:
        task_id = task_id or self.active_task
        if not task_id:
            rows = self.task_table.selectionModel().selectedRows() \
                if self.task_table.selectionModel() else []
            if rows:
                task_id = self._row_task_id(rows[0].row())
        if not task_id:
            QMessageBox.information(self, "提示", "请先选择任务")
            return
        review_dir = self.store.stage_dir(task_id, "review")
        if not (review_dir / "pending.json").exists():
            QMessageBox.information(self, "提示", "该任务没有待审核文案")
            return

        dlg = ReviewDialog(task_id, review_dir, self)
        if dlg.exec():
            self._append_log("审核通过，继续执行…")
            state = self.store.load(task_id)
            st = state.stage("review")
            st.status = StageStatus.DONE
            st.error = None
            self.store.save(state)
            self._task_cache = None
            self.b_review.setEnabled(False)
            self._run_task(task_id)
        else:
            # 「返回修改」：worker 已退出，若不复位则“开始任务”永久禁用
            self._append_log("已关闭审核窗口（文案保存在 review/pending.json，可随时重开）")
            self._reset_controls_after_pause()

    def _row_task_id(self, row: int) -> str | None:
        item = self.task_table.item(row, 0)
        if item is None:
            return None
        tid = item.data(Qt.ItemDataRole.UserRole)
        return str(tid) if tid else item.text()

    def _on_task_double_click(self, item: QTableWidgetItem) -> None:
        tid = self._row_task_id(item.row())
        if tid:
            self._show_task_detail(tid)

    def _show_task_detail(self, task_id: str) -> None:
        try:
            state = self.store.load(task_id)
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "任务详情", f"加载失败: {e}")
            return
        lines = [f"任务: {task_id}", f"名称: {state.name}",
                 f"阶段: {state.current_stage or '-'}", ""]
        for s in STAGE_ORDER:
            st = state.stage(s)
            mark = {"done": "✔", "failed": "✖", "review": "⏸",
                    "running": "▶", "skipped": "→"}.get(st.status.value, "·")
            lines.append(f"  {mark} {st.value:14s} {st.status.value}")
            if st.error:
                lines.append(f"      {st.error}")
        QMessageBox.information(self, "任务详情", "\n".join(lines))

    def _selected_task_id(self, pos=None) -> str | None:
        if pos is None:
            row = self.task_table.currentRow()
        else:
            # customContextMenuRequested 给的是视口坐标 QPoint；
            # QTableWidget.rowAt 只收 int y，传 QPoint 会 TypeError，
            # 而 slot 内未捕获异常会让 PyQt6 直接 abort（表现为“右键即退出”）
            row = self.task_table.rowAt(pos.y())
            if row >= 0:
                self.task_table.selectRow(row)
        if row < 0:
            return None
        return self._row_task_id(row)

    def _on_task_menu(self, pos) -> None:
        # slot 内任何未捕获异常都会让 PyQt6 qFatal 退出 → 整体兜底
        try:
            self._task_menu_impl(pos)
        except Exception as e:  # noqa: BLE001
            logger.exception("任务右键菜单异常: {}", e)

    def _task_menu_impl(self, pos) -> None:
        task_id = self._selected_task_id(pos)
        if not task_id:
            return
        try:
            state = self.store.load(task_id)
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "任务", f"加载失败: {e}")
            return
        busy = bool(self.worker and self.worker.isRunning())
        running = self._is_task_running(state)
        live = running and busy                    # 真正在执行
        stuck = running and not busy               # 残留 RUNNING（中途退出）
        failed = any(
            state.stage(s).status == StageStatus.FAILED for s in STAGE_ORDER
        )

        menu = QMenu(self)
        a_run = menu.addAction("继续运行")
        a_rerun = menu.addAction("再次运行（失败重跑）")
        a_review = menu.addAction("审核文案…")
        a_detail = menu.addAction("任务详情")
        menu.addSeparator()
        a_rename = menu.addAction("重命名…")
        a_arch = menu.addAction("取消归档" if state.archived else "归档")
        a_del = menu.addAction("删除…")
        a_run.setEnabled(not busy and not state.finished)
        a_rerun.setEnabled(not busy and not state.finished and (failed or stuck))
        a_rename.setEnabled(not live)
        a_arch.setEnabled(not live)
        a_del.setEnabled(not live)
        chosen = menu.exec(self.task_table.viewport().mapToGlobal(pos))
        if chosen is None:
            return
        if chosen is a_run:
            if busy:
                QMessageBox.information(self, "提示", "已有任务在运行")
                return
            if state.finished:
                QMessageBox.information(self, "提示", "该任务已完成")
                return
            self.active_task = task_id
            self._run_task(task_id)
        elif chosen is a_rerun:
            self._rerun_task(task_id)
        elif chosen is a_review:
            self.active_task = task_id
            self._open_review(task_id)
        elif chosen is a_detail:
            self._show_task_detail(task_id)
        elif chosen is a_rename:
            self._rename_task(task_id, state.name)
        elif chosen is a_arch:
            self._toggle_archive(task_id, not state.archived)
        elif chosen is a_del:
            self._delete_task(task_id, state.name)

    @staticmethod
    def _is_task_running(state) -> bool:
        return any(
            state.stage(s).status == StageStatus.RUNNING for s in STAGE_ORDER
        )

    def _rename_task(self, task_id: str, old_name: str) -> None:
        text, ok = QInputDialog.getText(
            self, "重命名任务", "新名称:", text=old_name
        )
        if not ok:
            return
        text = text.strip()
        if not text or text == old_name:
            return
        try:
            self.store.rename(task_id, text)
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "重命名失败", str(e))
            return
        self._append_log(f"任务已重命名: {task_id} -> {text}")
        self._task_cache = None
        self._refresh_tasks()

    def _toggle_archive(self, task_id: str, archived: bool) -> None:
        try:
            self.store.set_archived(task_id, archived)
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "归档失败", str(e))
            return
        if task_id in self.auto_queue and archived:
            self.auto_queue.remove(task_id)
        self._append_log(
            f"任务已{'归档' if archived else '取消归档'}: {task_id}"
            "（归档任务在状态筛选「已归档」中查看）"
        )
        self._task_cache = None
        self._refresh_tasks()

    def _delete_selected(self) -> None:
        """Delete 键：仅当焦点在任务表时删除选中行（避免吞掉文本框的删除键）。"""
        fw = QApplication.focusWidget()
        if fw is None or not self.task_table.isAncestorOf(fw):
            return
        tid = self._selected_task_id()
        if not tid:
            return
        try:
            name = self.store.load(tid).name
        except Exception:  # noqa: BLE001
            name = tid
        self._delete_task(tid, name)

    def _delete_task(self, task_id: str, name: str) -> None:
        ans = QMessageBox.question(
            self, "删除任务",
            f"确定删除「{name}」？\n任务目录与全部产物将被移除，不可恢复。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if ans != QMessageBox.StandardButton.Yes:
            return
        # rmtree 后台执行（大目录不卡 UI）；运行中任务不允许走到这里
        self.statusBar().showMessage(f"正在删除 {task_id} …")
        w = DeleteWorker(self.store, task_id, parent=self)
        w.succeeded.connect(self._on_task_deleted)
        w.failed.connect(self._on_task_delete_failed)
        w.start()

    def _on_task_deleted(self, task_id: str) -> None:
        if task_id in self.auto_queue:
            self.auto_queue.remove(task_id)
        if self.active_task == task_id:
            self.active_task = None
        self.statusBar().clearMessage()
        self._append_log(f"任务已删除: {task_id}")
        self._task_cache = None
        self._refresh_tasks()

    def _on_task_delete_failed(self, task_id: str, err: str) -> None:
        self.statusBar().clearMessage()
        logger.warning("删除任务失败 {}: {}", task_id, err)
        QMessageBox.warning(self, "删除失败", err)

    # ------------------------------------------------------------------
    # 失败任务再次运行
    # ------------------------------------------------------------------
    @staticmethod
    def _rerun_candidates(state) -> list[str]:
        """可复位重跑的阶段：FAILED 或残留 RUNNING（无进程时视为卡死）。"""
        return [
            s.value for s in STAGE_ORDER
            if state.stage(s).status in (StageStatus.FAILED, StageStatus.RUNNING)
        ]

    def _rerun_task(self, task_id: str) -> None:
        """失败任务从失败阶段重跑（已完成阶段自动跳过）。"""
        if self.worker and self.worker.isRunning():
            QMessageBox.information(self, "提示", "已有任务在运行")
            return
        try:
            state = self.store.load(task_id)
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "再次运行", f"加载失败: {e}")
            return
        if state.finished:
            QMessageBox.information(self, "提示", "该任务已完成")
            return
        reset = self._rerun_candidates(state)
        if not reset:
            QMessageBox.information(self, "提示", "该任务没有失败/卡住的阶段")
            return
        for key in reset:
            st = state.stage(key)
            st.status = StageStatus.PENDING
            st.error = None
            st.finished_at = None
        state.current_stage = None
        # 配音引擎/音色取面板当前选择（旧任务存的可能是已不可用的 edge 音色）
        state.project.tts_engine = self._current_tts_engine()
        state.project.voice = self.cb_voice.currentText().strip()
        try:
            self.store.save(state)
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "再次运行", f"保存状态失败: {e}")
            return
        self.active_task = task_id
        self._task_cache = None
        self._append_log(
            f"再次运行 {task_id}：已复位阶段 {', '.join(reset)}，"
            f"配音引擎 {state.project.tts_engine or '全局默认'}，"
            "从首个未完成阶段继续（已完成阶段自动跳过）"
        )
        self._run_task(task_id)

    def _on_selection_changed(self) -> None:
        """表格选择变化：更新「再次运行」按钮 + 成片预览。"""
        self._update_rerun_btn()
        self._update_preview()

    def _update_preview(self) -> None:
        """载入选中任务（无选中时回退到活动任务）的成片预览。"""
        task_id = self._selected_task_id() or self.active_task
        if not task_id:
            self.preview.load(None)
            return
        state = next(
            (t for t in self._load_tasks() if t.task_id == task_id), None
        )
        if state is None:
            self.preview.load(None)
            return
        out = (state.stage("export").data or {}).get("output")
        self.preview.load(out, on_open_dir=self._open_dir)

    def _update_rerun_btn(self) -> None:
        """「再次运行」按钮：选中任务存在失败/卡死阶段且当前空闲时可用。"""
        enabled = False
        task_id = self._selected_task_id()
        if task_id and not (self.worker and self.worker.isRunning()):
            # 复用刷新时已加载的 state，避免每次选择变化都再 load 一次
            state = next(
                (t for t in self._load_tasks() if t.task_id == task_id), None
            )
            if state and not state.finished and self._rerun_candidates(state):
                enabled = True
        self.b_rerun.setEnabled(enabled)

    def _rerun_selected(self) -> None:
        task_id = self._selected_task_id()
        if task_id:
            self._rerun_task(task_id)

    # ------------------------------------------------------------------
    # 批量队列
    # ------------------------------------------------------------------
    def _open_batch(self) -> None:
        if self.worker and self.worker.isRunning():
            QMessageBox.information(self, "提示", "已有任务在运行，请先停止")
            return
        files = self._pool_files()
        if len(files) < 2:
            QMessageBox.information(
                self, "批量队列", "请先向素材池添加至少 2 个视频文件"
            )
            return
        dlg = BatchDialog(
            files, store=self.store, project=self._collect_project(),
            default_prefix=self.ed_name.text().strip() + "_" if self.ed_name.text().strip() else "",
            default_mode=(MODE_PER_DIR if self._pool_origin() == {"dir"}
                          else MODE_PER_FILE),
            parent=self,
        )
        if not dlg.exec() or not dlg.created_ids:
            return
        self._task_cache = None                    # 对话框期间已 create
        ids = dlg.created_ids
        names = [self.store.load(i).name for i in ids]
        self._append_log(f"批量创建 {len(ids)} 个任务: " + ", ".join(names[:6])
                         + ("…" if len(names) > 6 else ""))
        if dlg.auto_run:
            self.auto_queue.extend(ids)
            self._append_log(f"排队执行已启动，共 {len(self.auto_queue)} 个待运行")
            self._refresh_tasks()
            if not (self.worker and self.worker.isRunning()):
                self._advance_queue()
        else:
            self._refresh_tasks()

    def _advance_queue(self) -> None:
        """取队首待运行任务并启动（不存在/已完成的自动跳过）。"""
        while self.auto_queue:
            nxt = self.auto_queue.pop(0)
            try:
                if not self.store.exists(nxt):
                    self._append_log(f"队列任务不存在，跳过: {nxt}")
                    continue
                state = self.store.load(nxt)
            except Exception as e:  # noqa: BLE001
                self._append_log(f"队列任务加载失败，跳过 {nxt}: {e}")
                continue
            if state.finished:
                continue
            self._append_log(
                f"队列开始下一任务（剩 {len(self.auto_queue)} 个）: "
                f"{state.name} [{nxt}]"
            )
            self.active_task = nxt
            self._run_task(nxt)
            self._refresh_tasks()
            return
        self._refresh_tasks()

    def _on_timer_tick(self) -> None:
        """定时刷新：worker 在跑且窗口可见时高频，否则降频。"""
        busy = bool(self.worker and self.worker.isRunning())
        interval = REFRESH_MS if busy and self.isVisible() else IDLE_REFRESH_MS
        if self.timer.interval() != interval:
            self.timer.setInterval(interval)
        # worker 会写盘 → 强制重读；空闲时直接复用缓存
        self._refresh_tasks(force=busy)

    def _schedule_search(self) -> None:
        """搜索防抖：连续输入只在停顿 200ms 后刷新（复用缓存，不读盘）。"""
        self.search_timer.start(SEARCH_DEBOUNCE_MS)

    def _load_tasks(self, *, force: bool = False) -> list:
        if force or self._task_cache is None:
            try:
                self._task_cache = self.store.list()
            except Exception as e:  # noqa: BLE001
                logger.warning("刷新任务列表失败: {}", e)
                self._task_cache = []
        return self._task_cache

    def _refresh_tasks(self, *, force: bool = False) -> None:
        tasks = self._load_tasks(force=force)

        active = 0
        waiting = 0
        has_review = False
        rows: list[tuple] = []
        query = self.ed_filter.text().strip().lower()
        view = self.cb_status_filter.currentText()

        for t in tasks:
            done = sum(1 for s in STAGE_ORDER if t.stage(s).status == StageStatus.DONE)
            review = t.stage("review").status == StageStatus.REVIEW
            status = derive_task_state(t)
            if review and not t.archived:
                has_review = True
            if status == "运行中":
                active += 1
            elif status in ("等待中", "待审核"):
                waiting += 1

            # 过滤：搜索（名称/ID）+ 状态视图（“全部”不含已归档）
            if query and query not in t.task_id.lower() and query not in t.name.lower():
                continue
            if view == "已归档":
                if not t.archived:
                    continue
            elif view == "全部":
                if t.archived:
                    continue
            elif status != view:
                continue
            rows.append((t, status, done))

        self.task_table.setRowCount(len(rows))
        for r, (t, status, done) in enumerate(rows):
            stage_disp = t.current_stage or "-"
            if t.current_stage:
                # 阶段内细分进度（如 understand 12/48帧）
                p = (t.stage(t.current_stage).data or {}).get("progress")
                if isinstance(p, dict) and p.get("total"):
                    stage_disp = (
                        f"{stage_disp} {p.get('done', 0)}/{p['total']}"
                        f"{p.get('label', '')}"
                    )
            vals = [t.name, str(len(t.sources)),
                    stage_disp, status,
                    f"{done}/{len(STAGE_ORDER)}"]
            for c, v in enumerate(vals):
                item = self.task_table.item(r, c)
                if item is None:
                    item = QTableWidgetItem()
                    self.task_table.setItem(r, c, item)
                item.setText(v)
            # 第 0 列主显任务名，ID 存入 UserRole + tooltip
            name_item = self.task_table.item(r, 0)
            name_item.setData(Qt.ItemDataRole.UserRole, t.task_id)
            name_item.setToolTip(f"ID: {t.task_id}")
            # 阶段格：tooltip 给全文（窄列截断时可悬停查看）
            self.task_table.item(r, 2).setToolTip(stage_disp)
            st_item = self.task_table.item(r, 3)
            # 状态格 tooltip：各阶段错误信息
            err_tip = "\n".join(
                f"{s.value}: {t.stage(s).error}"
                for s in STAGE_ORDER if t.stage(s).error
            )
            st_item.setToolTip(err_tip)
            st_item.setForeground(QBrush(QColor(STATUS_COLORS.get(status, "#1B1F27"))))
            st_font = st_item.font()
            st_font.setBold(True)
            st_item.setFont(st_font)
            if t.task_id == self.active_task:
                self.progress.setValue(done)

        # 有待审核任务时，“审核文案”按钮常可用（空闲状态下）
        if not (self.worker and self.worker.isRunning()):
            self.b_review.setEnabled(has_review)

        self.status_active.setText(f"● 活动中 {active}")
        self.status_waiting.setText(f"● 等待中 {waiting}")
        self.status_queue.setText(f"● 队列 {len(self.auto_queue)}")
        self._update_rerun_btn()
        self._update_preview()

    # ------------------------------------------------------------------
    def _append_log(self, msg: str) -> None:
        self.log_dialog.append(msg)
        # 状态栏同步显示最新一条（单行截断，避免挤压右侧提示）
        line = " ".join(msg.split())[:120]
        self.status_log.setText(line)

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        if self.worker and self.worker.isRunning():
            if QMessageBox.question(
                self, "退出", "任务正在运行，确定退出？（将请求停止）",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            ) != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self.worker.stop()
            if not self.worker.wait(8000):
                # 线程仍在收尾：强行销毁会 abort，稍后再试
                QMessageBox.warning(self, "退出", "任务线程仍在收尾，请稍后再退出")
                event.ignore()
                return
        event.accept()


def run_app(config: AppConfig) -> int:
    """启动桌面界面。"""
    import sys

    from PyQt6.QtWidgets import QApplication

    app = QApplication(sys.argv)
    app.setApplicationName(APP_ID)
    app.setApplicationDisplayName(APP_NAME)
    app.setWindowIcon(app_icon())
    apply_theme(app, config.ui.theme)
    win = MainWindow(config)

    # 把 loguru 输出（ffmpeg 警告、TTS 进度、BGM 缺失等）转发到界面日志
    sink_id = logger.add(
        lambda m: win.log_bridge.line.emit(str(m).rstrip("\n")),
        level="INFO",
        format="{message}",
        backtrace=False,
        colorize=False,
    )
    win.show()
    try:
        return app.exec()
    finally:
        logger.remove(sink_id)
