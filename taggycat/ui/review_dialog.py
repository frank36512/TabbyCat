"""文案人工审核对话框：查看/编辑 pending.json 后通过。"""

from __future__ import annotations

import json
from pathlib import Path

from PyQt6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from taggycat.pipeline.segments_io import load_segments, write_segments


class ReviewDialog(QDialog):
    """展示待审核文案，支持通过下方编辑框精修；确定即写回 approved。"""

    def __init__(self, task_id: str, review_dir: Path, parent=None):
        super().__init__(parent)
        self.task_id = task_id
        self.review_dir = review_dir
        self.pending_file = review_dir / "pending.json"
        self.setWindowTitle(f"文案审核 — {task_id}")
        self.resize(900, 600)

        self.segs = load_segments(self.pending_file)

        lay = QVBoxLayout(self)
        lay.addWidget(QLabel(
            f"共 {len(self.segs)} 段文案。修改后点「审核通过」继续配音；"
            f"或用 CLI: taggycat task approve {task_id} --file <segments.json>"
        ))

        self.table = QTableWidget(len(self.segs), 4, self)
        self.table.setHorizontalHeaderLabels(["片段", "时长", "字数", "文案"])
        self.table.setColumnWidth(0, 80)
        self.table.setColumnWidth(1, 70)
        self.table.setColumnWidth(2, 60)
        # 文案仅由下方编辑框修改：禁止单元格直编，避免切换行时被静默覆盖
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        for r, s in enumerate(self.segs):
            self.table.setItem(r, 0, QTableWidgetItem(s.segment_id))
            self.table.setItem(r, 1, QTableWidgetItem(f"{s.clip_duration:.1f}s"))
            self.table.setItem(r, 2, QTableWidgetItem(str(len(s.caption))))
            self.table.setItem(r, 3, QTableWidgetItem(s.caption))
        lay.addWidget(self.table)

        # 选中行 → 下方编辑框精修
        edit_row = QHBoxLayout()
        edit_row.addWidget(QLabel("精修："))
        self.editor = QPlainTextEdit()
        self.editor.setPlaceholderText("选中上表某行后在此编辑文案")
        self.editor.setMaximumHeight(90)
        edit_row.addWidget(self.editor)
        lay.addLayout(edit_row)
        self.table.itemSelectionChanged.connect(self._load_row)

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel
        )
        btns.button(QDialogButtonBox.StandardButton.Save).setText("审核通过")
        btns.button(QDialogButtonBox.StandardButton.Cancel).setText("返回修改")
        btns.accepted.connect(self._approve)
        btns.rejected.connect(self.reject)
        lay.addWidget(btns)

        self._row = -1

    def _load_row(self) -> None:
        r = self.table.currentRow()
        if r < 0:
            return
        # 先把编辑框内容写回上一行
        self._flush_editor()
        self._row = r
        self.editor.setPlainText(self.segs[r].caption)

    def _flush_editor(self) -> None:
        if 0 <= self._row < len(self.segs):
            self.segs[self._row].caption = self.editor.toPlainText().strip()
            self.table.item(self._row, 3).setText(self.segs[self._row].caption)
            self.table.item(self._row, 2).setText(str(len(self.segs[self._row].caption)))

    def _approve(self) -> None:
        self._flush_editor()
        empty = [s.segment_id for s in self.segs if not s.caption.strip()]
        if empty and QMessageBox.question(
            self, "确认",
            f"{len(empty)} 段文案为空（{', '.join(empty[:5])}），仍要通过？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        ) != QMessageBox.StandardButton.Yes:
            return

        # 写回 script/match 产物，使后续阶段读到审核后的文案
        for stage in ("script", "match"):
            dst = self.review_dir.parent / stage / "segments.json"
            if dst.exists():
                write_segments(dst, self.segs)
        (self.review_dir / "approved.json").write_text(
            json.dumps({"auto": False, "edited": True}, ensure_ascii=False),
            encoding="utf-8",
        )
        self.accept()
