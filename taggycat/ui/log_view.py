"""运行日志对话框与跨线程日志桥。"""

from __future__ import annotations

from datetime import datetime

from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtGui import QTextCursor
from PyQt6.QtWidgets import (
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
)


class LogBridge(QObject):
    """任意线程（worker/loguru sink）→ 主线程的安全日志通道。"""

    line = pyqtSignal(str)


class LogViewDialog(QDialog):
    """运行日志窗口（隐藏时仍持续缓存输出）。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("运行日志")
        self.resize(760, 440)

        self.view = QPlainTextEdit()
        self.view.setObjectName("logView")
        self.view.setReadOnly(True)
        self.view.setMaximumBlockCount(2000)
        self.view.setPlaceholderText("运行日志…")

        v = QVBoxLayout(self)
        v.setContentsMargins(12, 12, 12, 12)
        v.setSpacing(8)

        lrow = QHBoxLayout()
        lb_log = QLabel("ffmpeg / TTS / 流水线输出")
        lb_log.setProperty("hint", True)
        lrow.addWidget(lb_log)
        lrow.addStretch(1)
        self.autoscroll = QCheckBox("自动滚动")
        self.autoscroll.setChecked(True)
        lrow.addWidget(self.autoscroll)
        b_clear = QPushButton("清空")
        b_clear.setProperty("class", "ghost")
        b_clear.clicked.connect(self.view.clear)
        lrow.addWidget(b_clear)
        v.addLayout(lrow)
        v.addWidget(self.view, 1)

    def append(self, msg: str) -> None:
        """追加一行（带时间戳），勾选自动滚动时滚到底部。"""
        self.view.appendPlainText(f"{datetime.now():%H:%M:%S}  {msg}")
        if self.autoscroll.isChecked():
            self.view.moveCursor(QTextCursor.MoveOperation.End)
