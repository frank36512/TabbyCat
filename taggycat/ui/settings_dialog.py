"""接口设置对话框：在 GUI 中编辑 config.toml 的 LLM/VLM/TTS 接口与语音配置。"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import requests
from PyQt6.QtCore import QUrl
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QStackedWidget,
    QStyle,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from taggycat.config import AppConfig, find_config, set_config_values
from taggycat.ui.workers import PreviewWorker, ProbeWorker

EDGE_VOICES = [
    "zh-CN-YunxiNeural", "zh-CN-YunyangNeural", "zh-CN-XiaoxiaoNeural",
    "zh-CN-YunjianNeural", "zh-CN-XiaoyiNeural", "zh-CN-YunxiaNeural",
    "zh-CN-liaoning-XiaobeiNeural", "zh-CN-shaanxi-XiaoniNeural",
]

# TTS 引擎键与界面显示名（配置中存键，如 "comfyui_clone"）
TTS_ENGINES = [
    ("edge", "edge（在线语音）"),
    ("comfyui", "comfyui（工作流 TTS）"),
    ("comfyui_clone", "comfyui_clone（语音克隆）"),
]


def fill_tts_engines(combo: QComboBox, current: str) -> None:
    """填充引擎下拉框（显示中文标签，数据存引擎键）。"""
    for key, label in TTS_ENGINES:
        combo.addItem(label, key)
    idx = combo.findData((current or "edge").strip())
    combo.setCurrentIndex(idx if idx >= 0 else 0)


def sync_voice_field(combo: QComboBox, engine: str, default_voice: str) -> None:
    """按引擎切换音色输入：仅 edge 需要音色名，ComfyUI 由工作流/参考音频决定。"""
    if (engine or "edge").strip() == "edge":
        combo.setEnabled(True)
        combo.setPlaceholderText("Edge 音色名，如 zh-CN-YunxiNeural")
        if not combo.currentText().strip() and default_voice:
            combo.setCurrentText(default_voice)
    else:
        combo.setEnabled(False)
        combo.setCurrentText("")
        combo.setPlaceholderText(
            "由克隆参考音频决定" if engine == "comfyui_clone" else "由工作流决定"
        )


def probe_openai(base_url: str, api_key: str = "", timeout: float = 8.0) -> str:
    """OpenAI 兼容接口连通性探测（GET /models）。失败抛异常。"""
    if not base_url.strip():
        raise ValueError("Base URL 为空")
    url = base_url.strip().rstrip("/") + "/models"
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    r = requests.get(url, headers=headers, timeout=timeout)
    r.raise_for_status()
    data = r.json().get("data", [])
    ids = [m.get("id", "?") for m in data[:5] if isinstance(m, dict)]
    msg = f"连接成功，共 {len(data)} 个模型"
    if ids:
        msg += f"（如 {', '.join(ids)}）"
    return msg


def probe_comfyui(base_url: str, timeout: float = 8.0) -> str:
    """ComfyUI 接口连通性探测（GET /system_stats）。失败抛异常。"""
    if not base_url.strip():
        raise ValueError("Base URL 为空")
    url = base_url.strip().rstrip("/") + "/system_stats"
    r = requests.get(url, timeout=timeout)
    r.raise_for_status()
    return "ComfyUI 连接成功"


class SettingsDialog(QDialog):
    """编辑接口相关配置，保存时按键路径写回 config.toml（保留注释）。"""

    def __init__(self, config: AppConfig, parent=None):
        super().__init__(parent)
        self.config = config
        self.path: Path = config.config_path or find_config()
        self.preview_worker: PreviewWorker | None = None
        self.setWindowTitle("接口设置")
        self.resize(720, 620)

        root = QVBoxLayout(self)
        hint = QLabel(
            f"配置文件: {self.path}\n"
            "保存即写回文件；对新任务与试听生效（运行中的任务不受影响）。"
        )
        hint.setWordWrap(True)
        root.addWidget(hint)

        tabs = QTabWidget()
        tabs.addTab(self._tab_llm(), "云端 LLM")
        tabs.addTab(self._tab_local(), "本地 LLM")
        tabs.addTab(self._tab_asr(), "台词转录 ASR")
        tabs.addTab(self._tab_vlm(), "画面理解 VLM")
        tabs.addTab(self._tab_tts(), "语音配置")
        root.addWidget(tabs, 1)

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel
        )
        btns.button(QDialogButtonBox.StandardButton.Save).setText("保存")
        btns.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        btns.accepted.connect(self._save)
        btns.rejected.connect(self.reject)
        root.addWidget(btns)

    # ------------------------------------------------------------------
    # 各页
    # ------------------------------------------------------------------
    def _tab_llm(self) -> QWidget:
        w = QWidget()
        f = QFormLayout(w)
        c = self.config.llm
        self.ed_llm_url = QLineEdit(c.base_url)
        self.ed_llm_url.setPlaceholderText("https://api.deepseek.com/v1")
        self.ed_llm_key = QLineEdit(c.api_key)
        self.ed_llm_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.ed_llm_model = QLineEdit(c.model)
        f.addRow("Base URL", self.ed_llm_url)
        f.addRow("API Key", self.ed_llm_key)
        f.addRow("模型", self.ed_llm_model)
        f.addRow("", self._probe_btn(
            lambda: probe_openai(self.ed_llm_url.text(), self.ed_llm_key.text())
        ))
        f.addRow(QLabel("无有效 Key 时自动回退到「本地 LLM」（[llm_local]）。"))
        return w

    def _tab_local(self) -> QWidget:
        w = QWidget()
        f = QFormLayout(w)
        c = self.config.llm_local
        self.ed_local_url = QLineEdit(c.base_url)
        self.ed_local_url.setPlaceholderText("http://127.0.0.1:11434/v1")
        self.ed_local_key = QLineEdit(c.api_key)
        self.ed_local_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.ed_local_model = QLineEdit(c.model)
        f.addRow("Base URL", self.ed_local_url)
        f.addRow("API Key", self.ed_local_key)
        f.addRow("模型", self.ed_local_model)
        f.addRow("", self._probe_btn(
            lambda: probe_openai(self.ed_local_url.text(), self.ed_local_key.text())
        ))
        f.addRow(QLabel("Ollama 等本地 OpenAI 兼容服务；改模型后需已 ollama pull。"))
        return w

    def _tab_asr(self) -> QWidget:
        w = QWidget()
        f = QFormLayout(w)
        c = self.config.asr
        self.chk_asr = QCheckBox("启用台词转录（关闭则仅依赖外挂/内嵌字幕）")
        self.chk_asr.setChecked(c.enabled)
        f.addRow(self.chk_asr)

        self.cb_asr_engine = QComboBox()
        self.cb_asr_engine.addItem("本地 faster-whisper", "faster_whisper")
        self.cb_asr_engine.addItem("ComfyUI 工作流", "comfyui")
        idx = self.cb_asr_engine.findData(c.engine)
        self.cb_asr_engine.setCurrentIndex(idx if idx >= 0 else 0)
        f.addRow("转录方式", self.cb_asr_engine)

        self.asr_stack = QStackedWidget()

        # -- 页 0：本地 faster-whisper --
        p0 = QWidget()
        f0 = QFormLayout(p0)
        self.ed_asr_model = QLineEdit(c.model)
        self.ed_asr_model.setPlaceholderText("large-v3 / medium / small …")
        self.cb_asr_device = QComboBox()
        self.cb_asr_device.addItems(["cuda", "cpu"])
        self.cb_asr_device.setCurrentText(c.device)
        f0.addRow("模型", self.ed_asr_model)
        f0.addRow("设备", self.cb_asr_device)
        f0.addRow(QLabel(
            "CUDA 需要 cublas64_12.dll（nvidia-cublas-cu12）；"
            "缺失时 doctor 会提示，可改用 ComfyUI 工作流。"
        ))
        self.asr_stack.addWidget(p0)

        # -- 页 1：ComfyUI 转录工作流 --
        p1 = QWidget()
        f1 = QFormLayout(p1)
        cc = c.comfyui
        self.ed_asr_comfy_url = QLineEdit(cc.base_url)
        self.ed_asr_comfy_workflow = QLineEdit(cc.workflow)
        self.ed_asr_audio_input = QLineEdit(cc.audio_input)
        self.ed_asr_audio_input.setPlaceholderText("留空自动识别 LoadAudio 节点")
        f1.addRow("Base URL", self.ed_asr_comfy_url)
        _, asr_wf_box = self._with_browse(
            self.ed_asr_comfy_workflow, "选择转录工作流 (API 格式)", "JSON (*.json)"
        )
        f1.addRow("工作流 JSON", asr_wf_box)
        f1.addRow("音频输入名", self.ed_asr_audio_input)
        self.sp_asr_maxmb = QSpinBox()
        self.sp_asr_maxmb.setRange(1, 1000)
        self.sp_asr_maxmb.setValue(int(cc.max_upload_mb or 90))
        self.sp_asr_maxmb.setSuffix(" MB")
        f1.addRow("单次上传上限", self.sp_asr_maxmb)
        f1.addRow("", self._probe_btn(
            lambda: probe_comfyui(self.ed_asr_comfy_url.text()), text="测试连接"
        ))
        f1.addRow(QLabel(
            "工作流需为 API 格式；输出带 JSON 时间戳最佳，"
            "纯文本输出会按字数比例分配时间；"
            "音频超过上传上限（ComfyUI 默认 100MB）会自动分段转录。"
        ))
        self.asr_stack.addWidget(p1)

        f.addRow("参数", self.asr_stack)
        self.cb_asr_engine.currentIndexChanged.connect(self._sync_asr_page)
        self._sync_asr_page()
        return w

    def _sync_asr_page(self) -> None:
        self.asr_stack.setCurrentIndex(
            1 if self.cb_asr_engine.currentData() == "comfyui" else 0
        )

    def _tab_vlm(self) -> QWidget:
        w = QWidget()
        f = QFormLayout(w)
        c = self.config.vlm
        self.chk_vlm = QCheckBox("启用画面理解（关闭则仅用台词轨）")
        self.chk_vlm.setChecked(c.enabled)
        f.addRow(self.chk_vlm)

        self.cb_vlm_provider = QComboBox()
        self.cb_vlm_provider.addItems(["OpenAI 兼容 API", "ComfyUI 工作流"])
        self.cb_vlm_provider.setCurrentText(
            "ComfyUI 工作流"
            if (c.provider or "").lower().startswith("comfyui")
            else "OpenAI 兼容 API"
        )
        f.addRow("理解方式", self.cb_vlm_provider)

        self.vlm_stack = QStackedWidget()

        # -- 页 0：OpenAI 兼容 API --
        p0 = QWidget()
        f0 = QFormLayout(p0)
        self.ed_vlm_url = QLineEdit(c.base_url)
        self.ed_vlm_key = QLineEdit(c.api_key)
        self.ed_vlm_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.ed_vlm_model = QLineEdit(c.model)
        f0.addRow("Base URL", self.ed_vlm_url)
        f0.addRow("API Key", self.ed_vlm_key)
        f0.addRow("模型", self.ed_vlm_model)
        f0.addRow("", self._probe_btn(
            lambda: probe_openai(self.ed_vlm_url.text(), self.ed_vlm_key.text())
        ))
        self.vlm_stack.addWidget(p0)

        # -- 页 1：ComfyUI 视觉理解工作流 --
        p1 = QWidget()
        f1 = QFormLayout(p1)
        cc = c.comfyui
        self.ed_vlm_comfy_url = QLineEdit(cc.base_url)
        self.ed_vlm_comfy_workflow = QLineEdit(cc.workflow)
        self.ed_vlm_img_input = QLineEdit(cc.image_input)
        self.ed_vlm_img_input.setPlaceholderText("留空自动识别 LoadImage 节点")
        self.ed_vlm_txt_input = QLineEdit(cc.text_input)
        self.ed_vlm_txt_input.setPlaceholderText("留空自动识别；无则用工作流内置提示词")
        f1.addRow("Base URL", self.ed_vlm_comfy_url)
        _, wf_box = self._with_browse(
            self.ed_vlm_comfy_workflow, "选择视觉理解工作流 (API 格式)", "JSON (*.json)"
        )
        f1.addRow("工作流 JSON", wf_box)
        f1.addRow("图像输入名", self.ed_vlm_img_input)
        f1.addRow("提示词输入名", self.ed_vlm_txt_input)
        f1.addRow("", self._probe_btn(
            lambda: probe_comfyui(self.ed_vlm_comfy_url.text()), text="测试连接"
        ))
        f1.addRow(QLabel(
            "工作流需为 API 格式（Save (API Format)），输出节点返回文本描述；"
            "每帧提交一次，帧数受「最多帧数」限制。"
        ))
        self.vlm_stack.addWidget(p1)

        f.addRow("参数", self.vlm_stack)
        self.cb_vlm_provider.currentTextChanged.connect(self._sync_vlm_page)
        self._sync_vlm_page()

        self.sp_vlm_frames = QSpinBox()
        self.sp_vlm_frames.setRange(1, 1000)
        self.sp_vlm_frames.setValue(c.max_frames)
        f.addRow("最多帧数/任务", self.sp_vlm_frames)
        return w

    def _sync_vlm_page(self) -> None:
        self.vlm_stack.setCurrentIndex(
            1 if self.cb_vlm_provider.currentText() == "ComfyUI 工作流" else 0
        )

    def _tab_tts(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)

        head = QFormLayout()
        self.cb_tts_engine = QComboBox()
        fill_tts_engines(self.cb_tts_engine, self.config.tts.engine)
        self.cb_tts_engine.setToolTip(
            "edge=在线免费音色；comfyui=本地工作流 TTS；comfyui_clone=本地语音克隆"
        )
        head.addRow("默认引擎", self.cb_tts_engine)
        head.addRow(QLabel("此处即全局默认：保存后新建任务与「再次运行」都用它"))

        self.cb_tts_voice = QComboBox()
        self.cb_tts_voice.setEditable(True)
        self.cb_tts_voice.addItems(EDGE_VOICES)
        self.cb_tts_voice.setCurrentText(self.config.tts.default_voice)
        head.addRow("默认音色", self.cb_tts_voice)
        self.cb_tts_engine.currentIndexChanged.connect(
            lambda: sync_voice_field(
                self.cb_tts_voice,
                self.cb_tts_engine.currentData() or "",
                self.config.tts.default_voice,
            )
        )
        sync_voice_field(
            self.cb_tts_voice,
            self.cb_tts_engine.currentData() or "",
            self.config.tts.default_voice,
        )

        self.chk_tts_cache = QCheckBox("缓存配音音频（同文案不重复合成）")
        self.chk_tts_cache.setChecked(self.config.tts.cache)
        head.addRow("", self.chk_tts_cache)
        lay.addLayout(head)

        comfy = QGroupBox("ComfyUI 工作流 TTS")
        cf = QFormLayout(comfy)
        cc = self.config.tts.comfyui
        self.ed_comfy_url = QLineEdit(cc.base_url)
        self.ed_comfy_workflow = QLineEdit(cc.workflow)
        cf.addRow("Base URL", self.ed_comfy_url)
        _, comfy_box = self._with_browse(
            self.ed_comfy_workflow, "选择工作流 (API 格式)", "JSON (*.json)"
        )
        cf.addRow("工作流 JSON", comfy_box)
        lay.addWidget(comfy)

        clone = QGroupBox("ComfyUI 语音克隆")
        cl = QFormLayout(clone)
        ck = self.config.tts.comfyui_clone
        self.ed_clone_url = QLineEdit(ck.base_url)
        self.ed_clone_workflow = QLineEdit(ck.workflow)
        self.ed_clone_ref = QLineEdit(ck.reference_audio)
        cl.addRow("Base URL", self.ed_clone_url)
        _, clone_box = self._with_browse(
            self.ed_clone_workflow, "选择克隆工作流 (API 格式)", "JSON (*.json)"
        )
        cl.addRow("工作流 JSON", clone_box)
        _, ref_box = self._with_browse(
            self.ed_clone_ref, "选择参考音频", "音频 (*.wav *.mp3 *.flac *.m4a)"
        )
        cl.addRow("参考音频", ref_box)
        lay.addWidget(clone)

        btns = QHBoxLayout()
        self.b_preview = QPushButton("试听当前音色")
        self.b_preview.clicked.connect(self._preview_voice)
        btns.addWidget(self.b_preview)
        b_open_cache = QPushButton("打开缓存目录")
        b_open_cache.clicked.connect(self._open_cache_dir)
        btns.addWidget(b_open_cache)
        btns.addStretch(1)
        btns.addWidget(self._probe_btn(
            lambda: probe_comfyui(self.ed_comfy_url.text()), text="测试 ComfyUI"
        ))
        btns.addWidget(self._probe_btn(
            lambda: probe_comfyui(self.ed_clone_url.text()), text="测试克隆服务"
        ))
        lay.addLayout(btns)
        lay.addWidget(QLabel(
            "试听立即使用上方引擎/音色（无需先保存）；工作流等参数保存后生效。"
        ))
        lay.addWidget(QLabel(
            "工作流需为 API 格式（设置 → Dev mode → Save (API Format)）；"
            "文本/参考音频输入名留空可自动识别。"
        ))
        lay.addStretch(1)
        return w

    # ------------------------------------------------------------------
    def _probe_btn(self, fn: Callable[[], str], text: str = "测试连接") -> QPushButton:
        b = QPushButton(text)
        b.setProperty("orig_text", text)
        b.clicked.connect(lambda: self._start_probe(b, fn))
        return b

    def _start_probe(self, b: QPushButton, fn: Callable[[], str]) -> None:
        """探针在 QThread 中执行；期间按钮置灰显示“测试中…”。"""
        worker: ProbeWorker | None = b.property("worker")
        if worker is not None and worker.isRunning():
            return
        b.setEnabled(False)
        b.setText("测试中…")
        w = ProbeWorker(fn, parent=self)
        b.setProperty("worker", w)
        w.succeeded.connect(lambda msg: self._on_probe_result(b, True, msg))
        w.failed.connect(lambda msg: self._on_probe_result(b, False, msg))
        w.start()

    def _on_probe_result(self, b: QPushButton, ok: bool, msg: str) -> None:
        b.setEnabled(True)
        orig = b.property("orig_text") or "测试连接"
        b.setText(str(orig))
        if ok:
            QMessageBox.information(self, "连接成功", msg)
        else:
            QMessageBox.warning(self, "连接测试失败", msg)

    @staticmethod
    def _with_browse(
        ed: QLineEdit, title: str, filt: str
    ) -> tuple[QLineEdit, QWidget]:
        """返回 (原行编辑, 带…浏览按钮的容器)。"""
        box = QWidget()
        lay = QHBoxLayout(box)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(ed, 1)
        b = QPushButton()
        b.setFixedWidth(32)
        b.setIcon(
            b.style().standardIcon(QStyle.StandardPixmap.SP_DialogOpenButton)
        )
        b.setToolTip("浏览")

        def pick() -> None:
            path, _ = QFileDialog.getOpenFileName(box, title, "", filt)
            if path:
                ed.setText(path)

        b.clicked.connect(pick)
        lay.addWidget(b)
        return ed, box

    # ------------------------------------------------------------------
    # 试听 / 缓存目录
    # ------------------------------------------------------------------
    def _preview_voice(self) -> None:
        """后台合成样音（上方引擎 + 音色），完成后播放。"""
        if self.preview_worker and self.preview_worker.isRunning():
            return
        voice = self.cb_tts_voice.currentText().strip()
        engine = (self.cb_tts_engine.currentData() or "").strip()
        self.b_preview.setEnabled(False)
        w = PreviewWorker(
            self.config, "你好，这是虎猫解说的配音试听效果。",
            voice=voice, engine=engine,
        )
        w.done.connect(self._on_preview_done)
        w.failed.connect(self._on_preview_failed)
        self.preview_worker = w
        w.start()

    def _on_preview_done(self, path: str) -> None:
        self.b_preview.setEnabled(True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    def _on_preview_failed(self, msg: str) -> None:
        self.b_preview.setEnabled(True)
        QMessageBox.warning(self, "TTS 失败", msg)

    def _open_cache_dir(self) -> None:
        p = self.config.resolve(self.config.paths.tts_cache)
        p.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(p)))

    # ------------------------------------------------------------------
    def _collect(self) -> list[tuple[str, object]]:
        vlm_provider = (
            "comfyui"
            if self.cb_vlm_provider.currentText() == "ComfyUI 工作流"
            else "openai_compatible"
        )
        return [
            ("llm.base_url", self.ed_llm_url.text().strip()),
            ("llm.api_key", self.ed_llm_key.text().strip()),
            ("llm.model", self.ed_llm_model.text().strip()),
            ("llm_local.base_url", self.ed_local_url.text().strip()),
            ("llm_local.api_key", self.ed_local_key.text().strip()),
            ("llm_local.model", self.ed_local_model.text().strip()),
            ("asr.enabled", self.chk_asr.isChecked()),
            ("asr.engine", self.cb_asr_engine.currentData()),
            ("asr.model", self.ed_asr_model.text().strip()),
            ("asr.device", self.cb_asr_device.currentText()),
            ("asr.comfyui.base_url", self.ed_asr_comfy_url.text().strip()),
            ("asr.comfyui.workflow", self.ed_asr_comfy_workflow.text().strip()),
            ("asr.comfyui.audio_input", self.ed_asr_audio_input.text().strip()),
            ("asr.comfyui.max_upload_mb", float(self.sp_asr_maxmb.value())),
            ("vlm.enabled", self.chk_vlm.isChecked()),
            ("vlm.provider", vlm_provider),
            ("vlm.base_url", self.ed_vlm_url.text().strip()),
            ("vlm.api_key", self.ed_vlm_key.text().strip()),
            ("vlm.model", self.ed_vlm_model.text().strip()),
            ("vlm.max_frames", self.sp_vlm_frames.value()),
            ("vlm.comfyui.base_url", self.ed_vlm_comfy_url.text().strip()),
            ("vlm.comfyui.workflow", self.ed_vlm_comfy_workflow.text().strip()),
            ("vlm.comfyui.image_input", self.ed_vlm_img_input.text().strip()),
            ("vlm.comfyui.text_input", self.ed_vlm_txt_input.text().strip()),
            ("tts.engine", self.cb_tts_engine.currentData()),
            ("tts.default_voice", self.cb_tts_voice.currentText().strip()),
            ("tts.cache", self.chk_tts_cache.isChecked()),
            ("tts.comfyui.base_url", self.ed_comfy_url.text().strip()),
            ("tts.comfyui.workflow", self.ed_comfy_workflow.text().strip()),
            ("tts.comfyui_clone.base_url", self.ed_clone_url.text().strip()),
            ("tts.comfyui_clone.workflow", self.ed_clone_workflow.text().strip()),
            ("tts.comfyui_clone.reference_audio", self.ed_clone_ref.text().strip()),
        ]

    def _save(self) -> None:
        try:
            set_config_values(self.path, self._collect())
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "保存失败", str(e))
            return
        self.accept()

    def done(self, r: int) -> None:
        """关闭前等待试听线程结束（QThread 运行中被销毁会 abort）。"""
        if (
            self.preview_worker
            and self.preview_worker.isRunning()
            and not self.preview_worker.wait(5000)
        ):
            QMessageBox.warning(self, "试听", "试听仍在进行，请稍后再关闭")
            return
        super().done(r)
