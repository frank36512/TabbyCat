"""M5 UI 测试（QT_QPA_PLATFORM=offscreen，无显示环境可跑）。"""

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt6.QtWidgets", reason="未安装 UI 依赖 (extra: ui)")

from PyQt6.QtWidgets import QApplication  # noqa: E402

from taggycat.config import load_config  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
HAS_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


@pytest.fixture(scope="module")
def app():
    a = QApplication.instance() or QApplication([])
    yield a


@pytest.fixture
def config(tmp_path):
    cfg = load_config(ROOT / "config.example.toml")
    cfg.paths.workspace = str(tmp_path / "ws")
    cfg.paths.output = str(tmp_path / "out")
    cfg.asr.enabled = False
    cfg.vlm.enabled = False
    cfg.vlm.frame_interval = 1.0
    cfg.material.scene_threshold = 0.0
    return cfg


def _make_video(path: Path, duration: float = 2.0) -> str:
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-y",
         "-f", "lavfi", "-i", f"testsrc=duration={duration}:size=320x240:rate=10",
         "-f", "lavfi", "-i", f"sine=frequency=440:duration={duration}",
         "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
         "-c:a", "aac", str(path)],
        check=True, capture_output=True,
    )
    return str(path)


@pytest.fixture
def sample_video(tmp_path):
    if not HAS_FFMPEG:
        pytest.skip("需要 ffmpeg")
    return _make_video(tmp_path / "clip.mp4")


# ---------------------------------------------------------------------
# 主窗口
# ---------------------------------------------------------------------
def test_main_window_construct(app, config):
    from taggycat.ui.main_window import MainWindow

    win = MainWindow(config)
    assert win.pool.count() == 0
    assert win.cb_style.count() >= 4          # 解说风格预设（现有 15 种）
    assert win.log_dialog.windowTitle() == "运行日志"   # 日志在帮助菜单窗口
    assert win.sl_material.value() == 30      # 素材选取比例默认 30%
    assert win.sl_narration.value() == 50     # 解说比例默认 50%
    assert win.chk_validate.isChecked() is True
    assert win.task_table.columnCount() == 5
    assert "活动中" in win.status_active.text()
    assert "等待中" in win.status_waiting.text()
    win.close()


def test_pool_add_remove(app, config, sample_video):
    from taggycat.ui.main_window import MainWindow

    win = MainWindow(config)
    win._append_pool([sample_video])
    assert win.pool.count() == 1
    assert "1 个文件" in win.pool_hint.text()

    win._append_pool([sample_video])  # 去重
    assert win.pool.count() == 1

    # 逐个移除（offscreen 下 selectAll 不可靠，直接按行移除）
    while win.pool.count():
        win.pool.takeItem(0)
    win._remove_selected()   # 空池时应无异常
    assert win.pool.count() == 0
    win.close()


def test_collect_project(app, config):
    from taggycat.ui.main_window import MainWindow

    win = MainWindow(config)
    win.ed_name.setText("测试任务")
    win.cb_style.setCurrentText("悬疑解说")
    win.cb_voice.setCurrentText("zh-CN-YunjianNeural")
    win.sl_material.setValue(40)
    win.sl_narration.setValue(60)
    win.sp_target.setValue(120)
    win.chk_validate.setChecked(False)

    p = win._collect_project()
    assert p.style == "悬疑解说"
    assert p.voice == "zh-CN-YunjianNeural"
    assert p.tts_engine == win.cb_tts_engine.currentData()
    assert p.material_ratio == 40
    assert p.narration_ratio == 60
    assert p.target_duration == 120
    assert p.validate_script is False
    win.close()


def test_task_tts_engine_selectable(app, config):
    """新建任务面板可选 ComfyUI 工作流 TTS / 语音克隆，并写入任务配置。"""
    from taggycat.ui.main_window import MainWindow

    win = MainWindow(config)
    combo = win.cb_tts_engine
    assert [combo.itemData(i) for i in range(combo.count())] == [
        "edge", "comfyui", "comfyui_clone",
    ]
    assert win.cb_voice.isEnabled() is True        # 默认 edge 可选音色

    combo.setCurrentIndex(combo.findData("comfyui_clone"))
    assert win.cb_voice.isEnabled() is False
    assert "参考音频" in win.cb_voice.placeholderText()
    p = win._collect_project()
    assert p.tts_engine == "comfyui_clone"
    assert p.voice == ""                           # 音色由参考音频决定

    combo.setCurrentIndex(combo.findData("comfyui"))
    assert win._collect_project().tts_engine == "comfyui"

    combo.setCurrentIndex(combo.findData("edge"))
    assert win.cb_voice.isEnabled() is True
    assert win.cb_voice.currentText() == config.tts.default_voice
    win.close()


def test_refresh_tasks(app, config, sample_video):
    from taggycat.models import ProjectConfig
    from taggycat.store import TaskStore
    from taggycat.ui.main_window import MainWindow

    store = TaskStore(config)
    store.create([sample_video], name="待处理",
                 project=ProjectConfig(output_dir=str(Path(config.paths.output))))
    win = MainWindow(config)
    win._refresh_tasks(force=True)
    assert win.task_table.rowCount() == 1
    assert win.task_table.item(0, 3).text() == "等待中"
    assert "1" in win.status_waiting.text()
    win.close()


# ---------------------------------------------------------------------
# 审核对话框
# ---------------------------------------------------------------------
def test_review_dialog_approve(app, tmp_path):
    from taggycat.ui.review_dialog import ReviewDialog

    review_dir = tmp_path / "review"
    review_dir.mkdir()
    segs = [{"segment_id": "s001", "source_id": "a", "start": 0, "end": 4,
             "caption": "旧文案"}]
    (review_dir / "pending.json").write_text(
        json.dumps(segs, ensure_ascii=False), encoding="utf-8"
    )
    for stage in ("script", "match"):
        d = tmp_path / stage
        d.mkdir()
        (d / "segments.json").write_text(json.dumps(segs), encoding="utf-8")

    dlg = ReviewDialog("t1", review_dir)
    assert dlg.table.rowCount() == 1
    dlg.table.selectRow(0)
    dlg.editor.setPlainText("修改后的新文案")
    dlg._approve()   # 内部不进事件循环（除非有空文案确认框）

    approved = json.loads((review_dir / "approved.json").read_text(encoding="utf-8"))
    assert approved["edited"] is True
    written = json.loads(
        (tmp_path / "match" / "segments.json").read_text(encoding="utf-8")
    )
    assert written[0]["caption"] == "修改后的新文案"


def test_review_dialog_no_cell_edit_and_switch_keeps_text(app, tmp_path):
    """单元格不可直编；编辑框修改后切换行再切回，内容不丢失（防静默覆盖回归）。"""
    from taggycat.ui.review_dialog import ReviewDialog

    review_dir = tmp_path / "review"
    review_dir.mkdir()
    segs = [
        {"segment_id": "s001", "source_id": "a", "start": 0, "end": 4,
         "caption": "第一段文案"},
        {"segment_id": "s002", "source_id": "a", "start": 4, "end": 8,
         "caption": "第二段文案"},
    ]
    (review_dir / "pending.json").write_text(
        json.dumps(segs, ensure_ascii=False), encoding="utf-8"
    )

    dlg = ReviewDialog("t1", review_dir)
    assert dlg.table.editTriggers() == dlg.table.EditTrigger.NoEditTriggers

    dlg.table.selectRow(0)
    dlg.editor.setPlainText("第一段已修改")
    dlg.table.selectRow(1)                       # 切换行触发 flush + load
    assert dlg.segs[0].caption == "第一段已修改"
    assert dlg.editor.toPlainText() == "第二段文案"
    dlg.table.selectRow(0)                       # 切回
    assert dlg.editor.toPlainText() == "第一段已修改"


# ---------------------------------------------------------------------
# Worker 信号桥
# ---------------------------------------------------------------------
def test_worker_emits_stage_signals(app, config, monkeypatch, tmp_path):
    if not HAS_FFMPEG:
        pytest.skip("需要 ffmpeg")
    from taggycat.models import STAGE_ORDER, ProjectConfig, StageStatus
    from taggycat.store import TaskStore
    from taggycat.ui import workers as W

    store = TaskStore(config)
    vid = _make_video(tmp_path / "w.mp4")
    st = store.create([vid], name="worker",
                      project=ProjectConfig(output_dir=str(tmp_path / "out")))

    class StubPipeline:
        """替身流水线：只发事件 + 标记完成。"""

        def __init__(self, s):
            self.store = s

        def run(self, tid, *, on_stage=None, should_stop=None, **kw):
            if on_stage:
                on_stage("probe", "start")
                on_stage("probe", "done")
                on_stage("understand", "start")
                on_stage("understand", "done")
            state = self.store.load(tid)
            for s in STAGE_ORDER:
                state.stage(s).status = StageStatus.DONE
            state.finished = True
            state.current_stage = None
            self.store.save(state)
            return state

    monkeypatch.setattr(W, "build_pipeline", lambda cfg: StubPipeline(store))

    events: list[tuple[str, str]] = []
    logs: list[str] = []
    done: list[str] = []
    w = W.PipelineWorker(config, st.task_id)
    w.stage_event.connect(lambda s, e: events.append((s, e)))
    w.log.connect(logs.append)
    w.completed.connect(done.append)
    w.start()
    # 跨线程信号排队到主线程：等待期间必须泵事件循环，否则槽函数不执行
    deadline = time.time() + 30
    while not w.wait(50) and time.time() < deadline:
        app.processEvents()
    app.processEvents()   # 把最后一批排队信号 flush 掉
    assert not w.isRunning(), "worker 未在 30s 内结束"

    assert ("probe", "done") in events
    assert ("understand", "done") in events
    assert done == [st.task_id]
    assert any("任务完成" in m for m in logs)


def test_worker_stop_sets_flag(app, config, tmp_path):
    from taggycat.ui import workers as W

    w = W.PipelineWorker(config, "no-such-task")
    assert not w._stop.is_set()
    w.stop()
    assert w._stop.is_set()


def test_worker_catches_unexpected_exception(app, config, monkeypatch):
    """非 StageError 异常必须被兜底成 failed 信号，否则 PyQt6 会 abort 进程。"""
    from taggycat.ui import workers as W

    class Boom:
        def run(self, *a, **k):
            raise RuntimeError("意外炸了")

    monkeypatch.setattr(W, "build_pipeline", lambda cfg: Boom())
    w = W.PipelineWorker(config, "t-boom")
    msgs: list[str] = []
    w.failed.connect(msgs.append)
    w.start()
    deadline = time.time() + 15
    while not w.wait(50) and time.time() < deadline:
        app.processEvents()
    app.processEvents()
    assert not w.isRunning()
    assert msgs and "内部错误" in msgs[0] and "意外炸了" in msgs[0]


def test_review_reject_resets_buttons(app, config, sample_video, monkeypatch):
    """审核弹窗点「返回修改」后必须复位按钮，否则“开始任务”永久禁用。"""
    from taggycat.models import ProjectConfig, StageStatus
    from taggycat.store import TaskStore
    from taggycat.ui.main_window import MainWindow

    store = TaskStore(config)
    st = store.create([sample_video], name="待审核",
                      project=ProjectConfig(output_dir=str(Path(config.paths.output))))
    state = store.load(st.task_id)
    state.stage("review").status = StageStatus.REVIEW
    store.save(state)
    review_dir = store.stage_dir(st.task_id, "review")
    review_dir.mkdir(parents=True, exist_ok=True)
    (review_dir / "pending.json").write_text("[]", encoding="utf-8")

    win = MainWindow(config)
    win.b_start.setEnabled(False)
    win.b_stop.setEnabled(True)
    win.b_review.setEnabled(False)

    class RejectDialog:
        def __init__(self, *a, **k):
            pass

        def exec(self) -> bool:
            return False

    monkeypatch.setattr("taggycat.ui.main_window.ReviewDialog", RejectDialog)
    win._on_review_required(st.task_id)

    assert win.b_start.isEnabled(), "审核拒绝后「开始任务」应恢复可用"
    assert not win.b_stop.isEnabled()
    assert win.b_review.isEnabled(), "仍待审核，「审核文案」应保持可用"
    win.close()


def test_review_approve_continues(app, config, sample_video, monkeypatch):
    """审核通过 → 置 review=DONE 并重新拉起运行。"""
    from taggycat.models import ProjectConfig, StageStatus
    from taggycat.store import TaskStore
    from taggycat.ui.main_window import MainWindow

    store = TaskStore(config)
    st = store.create([sample_video], name="通过审核",
                      project=ProjectConfig(output_dir=str(Path(config.paths.output))))
    state = store.load(st.task_id)
    state.stage("review").status = StageStatus.REVIEW
    store.save(state)
    review_dir = store.stage_dir(st.task_id, "review")
    review_dir.mkdir(parents=True, exist_ok=True)
    (review_dir / "pending.json").write_text("[]", encoding="utf-8")

    win = MainWindow(config)
    ran: list[str] = []
    monkeypatch.setattr(win, "_run_task", lambda tid: ran.append(tid))

    class ApproveDialog:
        def __init__(self, *a, **k):
            pass

        def exec(self) -> bool:
            return True

    monkeypatch.setattr("taggycat.ui.main_window.ReviewDialog", ApproveDialog)
    win._on_review_required(st.task_id)

    assert ran == [st.task_id]
    assert store.load(st.task_id).stage("review").status == StageStatus.DONE
    win.close()


# ---------------------------------------------------------------------
# 编排器回调（UI 依赖的底层能力）
# ---------------------------------------------------------------------
def test_pipeline_on_stage_and_should_stop(config, sample_video, monkeypatch):
    from taggycat.models import ProjectConfig, StageStatus
    from taggycat.pipeline import build_pipeline
    from taggycat.store import TaskStore

    store = TaskStore(config)
    st = store.create([sample_video], name="cb",
                      project=ProjectConfig(output_dir=str(Path(config.paths.output))))
    p = build_pipeline(config)

    events: list[tuple[str, str]] = []
    final = p.run(st.task_id, until="understand",
                  on_stage=lambda s, e: events.append((s, e)))
    assert final.stages["probe"].status == StageStatus.DONE
    assert ("probe", "start") in events and ("probe", "done") in events
    assert ("subtitle", "start") in events

    # should_stop：第二个阶段前取消
    events2: list[tuple[str, str]] = []
    st2 = store.create([sample_video], name="stop",
                        project=ProjectConfig(output_dir=str(Path(config.paths.output))))
    calls = {"n": 0}

    def stopper() -> bool:
        calls["n"] += 1
        return calls["n"] > 1   # 第 1 个阶段放行，之后取消

    final2 = p.run(st2.task_id, on_stage=lambda s, e: events2.append((s, e)),
                   should_stop=stopper)
    assert [s for s, e in events2 if e == "done"] == ["probe"]
    assert final2.stages["probe"].status == StageStatus.DONE
    assert final2.current_stage == "subtitle"


# ---------------------------------------------------------------------
# 任务右键菜单 / 失败再次运行 / 阶段细分进度
# ---------------------------------------------------------------------
def _make_failed_task(win, tmp_path, name="failed"):
    from taggycat.models import StageStatus

    src = tmp_path / f"{name}.mp4"
    src.write_bytes(b"\x00" * 32)
    st = win.store.create([str(src)], name=name)
    st.stages["probe"].status = StageStatus.DONE
    a = st.stages["analyze"]
    a.status = StageStatus.FAILED
    a.error = "分析结果格式错误"
    st.current_stage = "analyze"
    win.store.save(st)
    return st


def _find_row(win, task_id: str) -> int:
    return next(
        r for r in range(win.task_table.rowCount())
        if win.task_table.item(r, 0).data(0x0100) == task_id   # Qt.UserRole
    )


def test_task_menu_rightclick_no_crash(app, config, tmp_path):
    """回归：右键任务行曾因 rowAt(QPoint) 抛 TypeError → PyQt6 abort（右键即退出）。"""
    from PyQt6.QtCore import QPoint
    from PyQt6.QtWidgets import QMenu

    from taggycat.ui.main_window import MainWindow

    win = MainWindow(config)
    _make_failed_task(win, tmp_path)
    win._refresh_tasks(force=True)
    assert win.task_table.rowCount() >= 1

    captured: dict = {}

    def _fake_exec(self, *a, **k):
        captured["actions"] = [x.text() for x in self.actions()]
        return None

    orig = QMenu.exec
    QMenu.exec = _fake_exec
    try:
        win.task_table.customContextMenuRequested.emit(QPoint(5, 5))
        win._on_task_menu(QPoint(5, 5))       # 修复前此处 TypeError
    finally:
        QMenu.exec = orig

    acts = captured.get("actions", [])
    assert "继续运行" in acts
    assert "再次运行（失败重跑）" in acts
    assert win.task_table.currentRow() >= 0   # 右键会选中该行
    win.close()


def test_rerun_failed_task(app, config, tmp_path, monkeypatch):
    from taggycat.models import StageStatus
    from taggycat.ui.main_window import MainWindow

    win = MainWindow(config)
    st = _make_failed_task(win, tmp_path, name="rerun_me")
    win._refresh_tasks(force=True)
    win.task_table.selectRow(_find_row(win, st.task_id))
    assert win.b_rerun.isEnabled(), "选中失败任务时「再次运行」应可用"

    ran: list[str] = []
    monkeypatch.setattr(win, "_run_task", lambda tid: ran.append(tid))
    # 旧任务存的是 edge：再次运行应改用面板当前选择的引擎
    old = win.store.load(st.task_id)
    old.project.tts_engine = "edge"
    old.project.voice = "zh-CN-YunxiNeural"
    win.store.save(old)
    win.cb_tts_engine.setCurrentIndex(win.cb_tts_engine.findData("comfyui_clone"))
    win._rerun_task(st.task_id)
    assert ran == [st.task_id]

    after = win.store.load(st.task_id)
    assert after.project.tts_engine == "comfyui_clone"   # 覆盖旧任务引擎
    assert after.project.voice == ""                     # 克隆音色由参考音频决定
    assert after.stages["analyze"].status == StageStatus.PENDING
    assert after.stages["analyze"].error is None
    assert after.stages["probe"].status == StageStatus.DONE   # 已完成阶段保留
    assert after.current_stage is None

    # 无失败/卡住阶段的任务：按钮不可用
    src = tmp_path / "waiting.mp4"
    src.write_bytes(b"\x00" * 32)
    st2 = win.store.create([str(src)], name="waiting")
    win._refresh_tasks(force=True)
    win.task_table.selectRow(_find_row(win, st2.task_id))
    assert not win.b_rerun.isEnabled()
    win.close()


def test_stage_progress_display(app, config, tmp_path):
    from taggycat.models import StageStatus
    from taggycat.ui.main_window import MainWindow

    win = MainWindow(config)
    src = tmp_path / "prog.mp4"
    src.write_bytes(b"\x00" * 32)
    st = win.store.create([str(src)], name="prog_disp")
    st.current_stage = "understand"
    u = st.stages["understand"]
    u.status = StageStatus.RUNNING
    u.data = {"progress": {"done": 12, "total": 48, "label": "帧"}}
    win.store.save(st)

    win._refresh_tasks(force=True)
    assert win.task_table.item(_find_row(win, st.task_id), 2).text() \
        == "understand 12/48帧"
    win.close()


def test_task_table_tooltips_columns_and_name(app, config, tmp_path):
    """TR-19.1: 名称主显+ID tooltip/UserRole；阶段列 Interactive+横滚恢复；错误入状态格 tooltip。"""
    from PyQt6.QtCore import Qt
    from PyQt6.QtWidgets import QHeaderView

    from taggycat.ui.main_window import MainWindow

    win = MainWindow(config)
    st = _make_failed_task(win, tmp_path, name="失败大作")
    win._refresh_tasks(force=True)
    r = _find_row(win, st.task_id)

    name_item = win.task_table.item(r, 0)
    assert name_item.text() == "失败大作"
    assert name_item.data(Qt.ItemDataRole.UserRole) == st.task_id
    assert st.task_id in name_item.toolTip()

    hdr = win.task_table.horizontalHeader()
    assert hdr.sectionResizeMode(2) == QHeaderView.ResizeMode.Interactive
    assert hdr.sectionResizeMode(3) == QHeaderView.ResizeMode.Interactive
    assert win.task_table.horizontalScrollBarPolicy() \
        == Qt.ScrollBarPolicy.ScrollBarAsNeeded

    assert win.task_table.item(r, 2).toolTip() == "analyze"
    err_tip = win.task_table.item(r, 3).toolTip()
    assert "analyze" in err_tip and "分析结果格式错误" in err_tip
    win.close()


def test_progress_bar_labels(app, config, tmp_path, monkeypatch):
    """TR-19.1: 进度条待机/任务名/失败停留三种标注。"""
    from taggycat.ui import main_window as MW

    monkeypatch.setattr(MW.QMessageBox, "warning",
                        staticmethod(lambda *a, **k: None))

    class _Sig:
        def connect(self, fn):  # noqa: ARG002
            return None

        def emit(self, *a):  # noqa: ARG002
            return None

    class _FakeWorker:
        def __init__(self, cfg, tid):
            self.tid = tid
            self.stage_event = _Sig()
            self.log = _Sig()
            self.failed = _Sig()
            self.review_required = _Sig()
            self.completed = _Sig()

        def start(self):
            pass

        def stop(self):
            pass

        def wait(self, ms=0):  # noqa: ARG002
            return True

        def isRunning(self):
            return False

    monkeypatch.setattr(MW, "PipelineWorker", _FakeWorker)

    win = MW.MainWindow(config)
    src = tmp_path / "bar.mp4"
    src.write_bytes(b"\x00" * 32)
    t = win.store.create([str(src)], name="样片任务")

    assert win.progress.format() == "待机"

    win._on_failed("已取消")
    assert win.progress.format() == "待机"

    win._on_failed("ffmpeg 炸了")
    assert win.progress.format() == "✖"
    assert win.progress.toolTip() == "ffmpeg 炸了"

    win._run_task(t.task_id)
    assert win.progress.format() == "样片任务 · %p%"
    win.close()


def test_search_uses_cache_and_debounce(app, config, tmp_path, monkeypatch):
    """TR-20.1: 搜索复用缓存不读盘、200ms 防抖；结果与强制刷新一致；空闲 timer 降频。"""
    from taggycat.ui.main_window import (
        IDLE_REFRESH_MS,
        MainWindow,
    )

    win = MainWindow(config)
    for nm in ("搜索目标甲", "普通任务乙", "目标丙丙"):
        src = tmp_path / f"{nm}.mp4"
        src.write_bytes(b"\x00" * 16)
        win.store.create([str(src)], name=nm)
    win._refresh_tasks(force=True)                 # 全量读一次

    list_calls: list[int] = []
    orig_list = win.store.list
    monkeypatch.setattr(
        win.store, "list",
        lambda: (list_calls.append(1) or orig_list()),
    )

    # 连续输入：防抖未触发，表格暂不变
    for ch in "目标":
        win.ed_filter.setText(win.ed_filter.text() + ch)
    assert win.task_table.rowCount() == 3

    win.search_timer.timeout.emit()                # 200ms 到点 → 内存过滤
    assert win.task_table.rowCount() == 2
    assert list_calls == []                        # 未碰磁盘
    names = {win.task_table.item(r, 0).text()
             for r in range(win.task_table.rowCount())}
    assert names == {"搜索目标甲", "目标丙丙"}

    # 强制刷新结果与内存过滤一致
    win._refresh_tasks(force=True)
    assert win.task_table.rowCount() == 2
    assert list_calls == [1]

    # 空闲无 worker → 定时刷新降频
    win._on_timer_tick()
    assert win.timer.interval() == IDLE_REFRESH_MS
    win.close()


# ---------------------------------------------------------------------
# 运行日志窗口 / 日志桥
# ---------------------------------------------------------------------
def test_log_view_dialog_append_clear_bridge(app):
    from PyQt6.QtWidgets import QPushButton

    from taggycat.ui.log_view import LogBridge, LogViewDialog

    dlg = LogViewDialog()
    dlg.append("hello")
    assert "hello" in dlg.view.toPlainText()

    # “清空”按钮
    b_clear = next(b for b in dlg.findChildren(QPushButton) if b.text() == "清空")
    b_clear.click()
    assert dlg.view.toPlainText() == ""

    # 跨线程桥信号
    got: list[str] = []
    bridge = LogBridge()
    bridge.line.connect(got.append)
    bridge.line.emit("x")
    assert got == ["x"]
    dlg.close()


def test_log_timestamp_and_autoscroll(app):
    """TR-21.1: 日志带时间戳；关闭自动滚动后追加不滚动。"""
    import re

    from PyQt6.QtGui import QTextCursor

    from taggycat.ui.log_view import LogViewDialog

    dlg = LogViewDialog()
    dlg.append("启动")
    text = dlg.view.toPlainText()
    assert re.search(r"\d{2}:\d{2}:\d{2}  启动", text)

    dlg.autoscroll.setChecked(False)
    dlg.view.moveCursor(QTextCursor.MoveOperation.Start)
    dlg.append("再一行")
    assert dlg.view.textCursor().position() != len(dlg.view.toPlainText())
    dlg.close()


def test_add_dir_recursive_scan(app, config, tmp_path, monkeypatch):
    """TR-21.1: 添加目录递归扫描子目录视频；无子视频时提示语含“子目录”。"""
    from taggycat.ui import main_window as MW

    nested = tmp_path / "movies" / "season1"
    nested.mkdir(parents=True)
    (nested / "ep1.mp4").write_bytes(b"\x00" * 8)
    (tmp_path / "movies" / "top.mkv").write_bytes(b"\x00" * 8)
    (nested / "note.txt").write_text("x", encoding="utf-8")

    win = MW.MainWindow(config)
    monkeypatch.setattr(
        MW.QFileDialog, "getExistingDirectory",
        staticmethod(lambda *a, **k: str(tmp_path / "movies")),
    )
    win._add_dir()
    assert win.pool.count() == 2

    win.pool.clear()
    empty_root = tmp_path / "empty"
    empty_root.mkdir()
    (empty_root / "sub").mkdir()
    boxes: list[str] = []
    monkeypatch.setattr(
        MW.QFileDialog, "getExistingDirectory",
        staticmethod(lambda *a, **k: str(empty_root)),
    )
    monkeypatch.setattr(
        MW.QMessageBox, "information",
        staticmethod(lambda *a, **k: boxes.append(a[2])),
    )
    win._add_dir()
    assert boxes and "子目录" in boxes[0]
    win.close()


def test_shortcuts_and_batch_toggle(app, config, tmp_path, monkeypatch):
    """TR-21.1: Ctrl+Enter/F5/Delete 快捷键存在；运行期间批量按钮置灰。"""
    from PyQt6.QtGui import QShortcut

    from taggycat.ui import main_window as MW

    class _Sig:
        def connect(self, fn):
            return None

    class _FakeWorker:
        def __init__(self, cfg, tid):
            self.stage_event = _Sig()
            self.log = _Sig()
            self.failed = _Sig()
            self.review_required = _Sig()
            self.completed = _Sig()

        def start(self):
            pass

        def stop(self):
            pass

        def wait(self, ms=0):
            return True

        def isRunning(self):
            return True

    monkeypatch.setattr(MW, "PipelineWorker", _FakeWorker)
    # FakeWorker.isRunning()=True → closeEvent 会弹确认，mock 为 Yes
    monkeypatch.setattr(
        MW.QMessageBox, "question",
        staticmethod(lambda *a, **k: MW.QMessageBox.StandardButton.Yes),
    )

    win = MW.MainWindow(config)
    seqs = {s.key().toString() for s in win.findChildren(QShortcut)}
    assert {"Ctrl+Return", "F5", "Del"} <= seqs

    assert win.b_batch.isEnabled()
    src = tmp_path / "s.mp4"
    src.write_bytes(b"\x00" * 8)
    t = win.store.create([str(src)], name="t")
    win.active_task = t.task_id
    win._run_task(t.task_id)
    assert not win.b_batch.isEnabled()
    win.close()


def test_video_preview_load_and_placeholder(app, tmp_path):
    """成片预览：加载已存在视频时切换到播放页并启用控件；无视频显示占位。"""
    from taggycat.ui.main_window import VideoPreview

    vp = VideoPreview()
    assert vp.stack.currentIndex() == 0               # 占位页
    assert not vp.b_play.isEnabled()

    vf = tmp_path / "out.mp4"
    vf.write_bytes(b"\x00" * 32)
    opened: list = []
    vp.load(vf, on_open_dir=lambda d: opened.append(d))
    assert vp.stack.currentIndex() == 1               # 视频页
    assert vp.b_play.isEnabled()
    assert Path(vp.player.source().toLocalFile()) == vf
    assert vp._output == vf

    vp.open_output_dir()
    assert opened == [vf.parent]

    vp.load(None)
    assert vp.stack.currentIndex() == 0
    assert not vp.b_play.isEnabled()
    assert vp._output is None


def test_preview_reload_same_path_noop(app, tmp_path):
    """同一视频重复 load 不重置播放（定时刷新不应打断播放）。"""
    from taggycat.ui.main_window import VideoPreview

    vp = VideoPreview()
    vf = tmp_path / "out.mp4"
    vf.write_bytes(b"\x00" * 32)
    vp.load(vf)
    calls: list = []
    orig = vp.player.setSource
    vp.player.setSource = lambda src: calls.append(src) or orig(src)
    vp.load(vf)                            # 同路径 → 不再 setSource
    assert calls == []
    assert vp._output == vf

    vf2 = tmp_path / "out2.mp4"
    vf2.write_bytes(b"\x00" * 32)
    vp.load(vf2)                           # 换路径 → 重新载入
    assert len(calls) == 1
    assert vp._output == vf2


def test_preview_follows_task_selection(app, config, tmp_path):
    """选中带 export output 的任务时预览载入成片；无输出的任务显示占位。"""
    from taggycat.models import StageStatus
    from taggycat.ui.main_window import MainWindow

    win = MainWindow(config)

    # 已完成任务，export 有输出
    src = tmp_path / "a.mp4"
    src.write_bytes(b"\x00" * 8)
    t = win.store.create([str(src)], name="成片A")
    from taggycat.models import STAGE_ORDER
    for s in STAGE_ORDER:
        t.stages[s].status = StageStatus.DONE
    out = tmp_path / "final.mp4"
    out.write_bytes(b"\x00" * 16)
    t.stages["export"].data = {"output": str(out)}
    t.finished = True
    win.store.save(t)

    # 未完成、无输出的任务
    src2 = tmp_path / "b.mp4"
    src2.write_bytes(b"\x00" * 8)
    t2 = win.store.create([str(src2)], name="未完成")

    win._refresh_tasks(force=True)

    # 选中成片任务 → 预览载入
    win.task_table.selectRow(_find_row(win, t.task_id))
    win._update_preview()
    assert win.preview.stack.currentIndex() == 1
    assert Path(win.preview.player.source().toLocalFile()) == out

    # 选中无输出任务 → 占位
    win.task_table.selectRow(_find_row(win, t2.task_id))
    win._update_preview()
    assert win.preview.stack.currentIndex() == 0
    assert win.preview._output is None
    win.close()
