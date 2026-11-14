"""批量建队列对话框：把素材池拆成多个任务并可自动排队执行。"""

from __future__ import annotations

from collections import OrderedDict
from pathlib import Path

from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from taggycat.models import ProjectConfig
from taggycat.store import TaskStore

MODE_PER_FILE = "每个文件一个任务"
MODE_PER_DIR = "按目录分组（每组一个任务）"


def build_plan(files: list[str], mode: str, prefix: str) -> list[tuple[str, list[str]]]:
    """按拆分方式返回 [(任务名, [素材路径…]), …]（重名自动加序号）。

    MODE_PER_DIR 按父目录分组（保持首次出现顺序）；否则每文件一组。
    批量对话框与主窗口「多目录开始任务」共用。
    """
    groups: OrderedDict[str, list[str]] = OrderedDict()
    if mode == MODE_PER_DIR:
        for f in files:
            groups.setdefault(str(Path(f).parent), []).append(f)
    else:
        for f in files:
            groups[f] = [f]

    seen: dict[str, int] = {}
    plan: list[tuple[str, list[str]]] = []
    for key, members in groups.items():
        base = prefix + (
            Path(key).name if mode == MODE_PER_DIR else Path(key).stem
        )
        n = seen.get(base, 0) + 1
        seen[base] = n
        name = base if n == 1 else f"{base}_{n}"
        plan.append((name, members))
    return plan


class BatchDialog(QDialog):
    """按模式把素材列表规划成多个任务，确认后统一创建。"""

    def __init__(
        self,
        files: list[str],
        *,
        store: TaskStore,
        project: ProjectConfig,
        default_prefix: str = "",
        default_mode: str | None = None,
        parent=None,
    ):
        super().__init__(parent)
        self.files = [str(Path(f)) for f in files]
        self.store = store
        self.project = project
        self.created_ids: list[str] = []
        self.auto_run = True

        self.setWindowTitle("批量创建队列")
        self.resize(640, 480)

        lay = QVBoxLayout(self)
        lay.addWidget(QLabel(
            f"素材池共 {len(self.files)} 个文件。选择拆分方式后将一次创建多个任务；"
            "勾选排队执行可自动逐个运行（运行中可停止中断）。"
        ))

        form = QFormLayout()
        self.cb_mode = QComboBox()
        self.cb_mode.addItems([MODE_PER_FILE, MODE_PER_DIR])
        if default_mode in (MODE_PER_FILE, MODE_PER_DIR):
            self.cb_mode.setCurrentText(default_mode)
        form.addRow("拆分方式", self.cb_mode)
        self.ed_prefix = QLineEdit(default_prefix)
        self.ed_prefix.setPlaceholderText("任务名前缀（可空），如：第01期_")
        form.addRow("任务名前缀", self.ed_prefix)
        self.chk_auto = QCheckBox("创建后自动排队执行（跑完一个自动开下一个）")
        self.chk_auto.setChecked(True)
        form.addRow("", self.chk_auto)
        lay.addLayout(form)

        self.plan_table = QTableWidget(0, 3)
        self.plan_table.setHorizontalHeaderLabels(["任务名", "素材数", "首个素材"])
        self.plan_table.setColumnWidth(0, 240)
        self.plan_table.setColumnWidth(1, 70)
        self.plan_table.setColumnWidth(2, 300)
        self.plan_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.plan_table.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows
        )
        lay.addWidget(self.plan_table, 1)

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        btns.button(QDialogButtonBox.StandardButton.Ok).setText("创建任务")
        btns.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        btns.accepted.connect(self._create)
        btns.rejected.connect(self.reject)
        lay.addWidget(btns)

        self.cb_mode.currentTextChanged.connect(self._refresh_plan)
        self.ed_prefix.textChanged.connect(self._refresh_plan)
        self._refresh_plan()

    # ------------------------------------------------------------------
    def _plan(self) -> list[tuple[str, list[str]]]:
        """返回 [(任务名, [素材路径…]), …]（重名自动加序号）。"""
        return build_plan(
            self.files, self.cb_mode.currentText(), self.ed_prefix.text().strip()
        )

    def _refresh_plan(self) -> None:
        plan = self._plan()
        self.plan_table.setRowCount(len(plan))
        for r, (name, group) in enumerate(plan):
            first = Path(group[0]).name
            if len(group) > 1:
                first += f" 等 {len(group)} 个"
            name_item = QTableWidgetItem(name)
            name_item.setToolTip("\n".join(Path(x).name for x in group))
            self.plan_table.setItem(r, 0, name_item)
            self.plan_table.setItem(r, 1, QTableWidgetItem(str(len(group))))
            first_item = QTableWidgetItem(first)
            first_item.setToolTip("\n".join(group))      # 完整路径
            self.plan_table.setItem(r, 2, first_item)

    # ------------------------------------------------------------------
    def _create(self) -> None:
        plan = self._plan()
        if not plan:
            QMessageBox.warning(self, "批量创建", "没有可创建的任务（素材池为空）")
            return
        created: list[str] = []
        try:
            for name, group in plan:
                st = self.store.create(group, name=name, project=self.project)
                created.append(st.task_id)
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(
                self, "批量创建中断",
                f"已创建 {len(created)} 个任务后失败:\n{e}",
            )
            if not created:
                return
        self.created_ids = created
        self.auto_run = self.chk_auto.isChecked()
        self.accept()
