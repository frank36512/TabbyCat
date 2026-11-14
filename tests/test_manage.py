"""任务管理（改名/归档/删除）+ 批量建队列 + 队列执行测试。"""

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt6.QtWidgets", reason="未安装 UI 依赖 (extra: ui)")

from PyQt6.QtWidgets import QPushButton  # noqa: E402

from taggycat.config import load_config  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def app():
    from PyQt6.QtWidgets import QApplication

    a = QApplication.instance() or QApplication([])
    yield a


@pytest.fixture
def config(tmp_path):
    cfg = load_config(ROOT / "config.example.toml")
    cfg.paths.workspace = str(tmp_path / "ws")
    cfg.paths.output = str(tmp_path / "out")
    return cfg


def _mk_files(tmp_path: Path, *rel: str) -> list[str]:
    out = []
    for r in rel:
        p = tmp_path / r
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"\x00" * 16)
        out.append(str(p))
    return out


def _mk_task(store, files, name="任务"):
    from taggycat.models import ProjectConfig

    return store.create(files, name=name,
                        project=ProjectConfig(output_dir=str(store.config.output_dir)))


# ---------------------------------------------------------------------
# 存储层
# ---------------------------------------------------------------------
def test_save_while_target_locked(config, tmp_path):
    """Windows 读句柄无删除共享 → os.replace 报 WinError 5：重试 + 直写兜底。"""
    from taggycat.store import TaskStore

    files = _mk_files(tmp_path, "a.mp4")
    store = TaskStore(config)
    st = _mk_task(store, files, name="锁测试")
    path = store.state_path(st.task_id)
    store._REPLACE_RETRIES = 2      # 测试提速
    store._REPLACE_DELAY = 0.01

    st.name = "改名了"
    with path.open("r", encoding="utf-8") as fh:   # 持有读句柄（无删除共享）
        store.save(st)                             # 不应抛 PermissionError
        fh.read()                                  # 旧句柄仍可用
    assert store.load(st.task_id).name == "改名了"
    assert not list(path.parent.glob("*.tmp"))     # tmp 已清理


def test_store_rename(config):
    from taggycat.store import TaskStore

    store = TaskStore(config)
    st = _mk_task(store, _mk_files(Path(config.paths.workspace).parent, "a.mp4"))
    store.rename(st.task_id, "新名字")
    assert store.load(st.task_id).name == "新名字"
    with pytest.raises(ValueError):
        store.rename(st.task_id, "   ")


def test_store_archive_toggle(config):
    from taggycat.store import TaskStore

    store = TaskStore(config)
    st = _mk_task(store, _mk_files(Path(config.paths.workspace).parent, "b.mp4"))
    assert store.load(st.task_id).archived is False
    store.set_archived(st.task_id, True)
    assert store.load(st.task_id).archived is True
    store.set_archived(st.task_id, False)
    assert store.load(st.task_id).archived is False


def test_store_delete(config):
    from taggycat.store import TaskStore

    store = TaskStore(config)
    st = _mk_task(store, _mk_files(Path(config.paths.workspace).parent, "c.mp4"))
    d = store.task_dir(st.task_id)
    assert d.exists()
    store.delete(st.task_id)
    assert not d.exists()
    with pytest.raises(FileNotFoundError):
        store.load(st.task_id)
    with pytest.raises(FileNotFoundError):
        store.delete(st.task_id)


def test_old_state_without_archived_key(config):
    """旧版 state.json（无 archived 字段）兼容加载。"""
    import json

    from taggycat.store import TaskStore

    store = TaskStore(config)
    st = _mk_task(store, _mk_files(Path(config.paths.workspace).parent, "d.mp4"))
    raw = json.loads(store.state_path(st.task_id).read_text(encoding="utf-8"))
    raw.pop("archived", None)
    store.state_path(st.task_id).write_text(
        json.dumps(raw, ensure_ascii=False), encoding="utf-8"
    )
    assert store.load(st.task_id).archived is False


# ---------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------
def test_cli_task_manage(tmp_path, capsys, monkeypatch):
    from taggycat.cli import main

    cfgp = tmp_path / "config.toml"
    cfgp.write_bytes((ROOT / "config.example.toml").read_bytes())
    f = str(tmp_path / "clip.mp4")
    Path(f).write_bytes(b"\x00" * 16)
    base = ["-c", str(cfgp), "task"]

    assert main(base + ["create", f, "--name", "甲"]) == 0
    assert main(base + ["rename", "甲", "乙"]) == 0
    out = capsys.readouterr().out
    assert "-> 乙" in out

    assert main(base + ["archive", "乙"]) == 0
    assert main(base + ["list"]) == 0
    out = capsys.readouterr().out
    assert "暂无任务" in out                      # 默认隐藏归档
    assert main(base + ["list", "--all"]) == 0
    out = capsys.readouterr().out
    assert "归档/" in out and "乙" in out

    assert main(base + ["list", "--json"]) == 0
    out = capsys.readouterr().out
    assert '"task_id"' not in out                 # 归档被过滤，无任务
    assert main(base + ["list", "--all", "--json"]) == 0
    out = capsys.readouterr().out
    assert '"archived": true' in out

    assert main(base + ["delete", "乙", "--yes"]) == 0
    assert main(base + ["list", "--all"]) == 0
    out = capsys.readouterr().out
    assert "暂无任务" in out


# ---------------------------------------------------------------------
# 批量对话框
# ---------------------------------------------------------------------
def test_batch_dialog_per_file(app, config, tmp_path):
    from taggycat.store import TaskStore
    from taggycat.ui.batch_dialog import MODE_PER_DIR, MODE_PER_FILE, BatchDialog

    files = _mk_files(
        tmp_path,
        "s1/ep01.mp4", "s1/ep02.mp4", "s1/ep03.mp4", "s2/movie.mp4",
    )
    store = TaskStore(config)
    from taggycat.models import ProjectConfig

    dlg = BatchDialog(files, store=store, project=ProjectConfig(),
                      default_prefix="P_")
    assert dlg.plan_table.rowCount() == 4          # 每文件一个
    dlg.cb_mode.setCurrentText(MODE_PER_DIR)
    assert dlg.plan_table.rowCount() == 2          # 按目录分组
    dlg.cb_mode.setCurrentText(MODE_PER_FILE)
    dlg.chk_auto.setChecked(False)
    dlg._create()

    assert dlg.result() == dlg.DialogCode.Accepted
    assert len(dlg.created_ids) == 4
    assert dlg.auto_run is False
    names = [store.load(i).name for i in dlg.created_ids]
    assert all(n.startswith("P_") for n in names)
    assert len(store.list()) == 4


def test_batch_dialog_duplicate_stem_gets_index(app, config, tmp_path):
    from taggycat.store import TaskStore
    from taggycat.ui.batch_dialog import BatchDialog

    files = _mk_files(tmp_path, "x/movie.mp4", "y/movie.mp4")
    store = TaskStore(config)
    from taggycat.models import ProjectConfig

    dlg = BatchDialog(files, store=store, project=ProjectConfig())
    names = [n for n, _ in dlg._plan()]
    assert len(set(names)) == 2                    # 同名自动加序号


def test_batch_dialog_default_mode(app, config, tmp_path):
    from taggycat.models import ProjectConfig
    from taggycat.store import TaskStore
    from taggycat.ui.batch_dialog import MODE_PER_DIR, MODE_PER_FILE, BatchDialog

    files = _mk_files(tmp_path, "s1/ep01.mp4", "s1/ep02.mp4", "s2/ep03.mp4")
    store = TaskStore(config)
    dlg = BatchDialog(files, store=store, project=ProjectConfig(),
                      default_mode=MODE_PER_DIR)
    assert dlg.cb_mode.currentText() == MODE_PER_DIR
    assert dlg.plan_table.rowCount() == 2          # 目录分组预览
    # TR-21.1: 预览表 tooltip（任务名格列成员、首件格完整路径）
    name_tip = dlg.plan_table.item(0, 0).toolTip()
    assert "ep01.mp4" in name_tip and "ep02.mp4" in name_tip
    assert dlg.plan_table.item(0, 2).toolTip()
    dlg2 = BatchDialog(files, store=store, project=ProjectConfig())
    assert dlg2.cb_mode.currentText() == MODE_PER_FILE  # 缺省每文件


# ---------------------------------------------------------------------
# 素材池来源：文件/目录互斥 + 多目录开始任务自动拆分
# ---------------------------------------------------------------------
def test_pool_mixed_origin_blocked(app, config, tmp_path, monkeypatch):
    from taggycat.ui import main_window as MW

    f_dir, f_file = _mk_files(tmp_path, "a/one.mp4", "two.mp4")
    warns: list[str] = []
    monkeypatch.setattr(
        MW.QMessageBox, "warning",
        staticmethod(lambda *a, **k: warns.append(str(a[2]) if len(a) > 2 else "")),
    )

    win = MW.MainWindow(config)
    win._append_pool([f_dir], origin="dir")        # 目录来源池
    monkeypatch.setattr(
        MW.QFileDialog, "getOpenFileNames",
        staticmethod(lambda *a, **k: ([f_file], "")),
    )
    win._add_files()                               # 应被拦截，不混入
    assert win.pool.count() == 1
    assert win._pool_origin() == {"dir"}
    assert any("不可混用" in w for w in warns)

    warns.clear()
    win2 = MW.MainWindow(config)
    win2._append_pool([f_file], origin="file")     # 文件来源池
    monkeypatch.setattr(
        MW.QFileDialog, "getExistingDirectory",
        staticmethod(lambda *a, **k: str(tmp_path / "a")),
    )
    win2._add_dir()                                # 应被拦截，不混入
    assert win2.pool.count() == 1
    assert win2._pool_origin() == {"file"}
    assert any("不可混用" in w for w in warns)
    win.close()
    win2.close()


def test_start_task_multi_dir_splits_and_queues(app, config, tmp_path, monkeypatch):
    from taggycat.ui.main_window import MainWindow

    files = _mk_files(tmp_path, "d1/ep01.mp4", "d1/ep02.mp4", "d2/ep03.mp4")
    win = MainWindow(config)
    win._append_pool(files, origin="dir")
    ran: list[str] = []
    monkeypatch.setattr(win, "_run_task", lambda tid: ran.append(tid))
    win._start_task()

    assert len(win.store.list()) == 2              # 按目录拆成 2 个任务
    names = [t.name for t in win.store.list()]
    assert any("d1" in n for n in names)
    assert any("d2" in n for n in names)
    assert len(ran) == 1                           # 队列只启动首个
    assert len(win.auto_queue) == 1                # 余 1 个待运行
    win.close()


def test_start_task_file_origin_merges_all(app, config, tmp_path, monkeypatch):
    from taggycat.ui.main_window import MainWindow

    files = _mk_files(tmp_path, "p1/x.mp4", "p2/y.mp4")  # 散选文件可跨目录
    win = MainWindow(config)
    win._append_pool(files)                        # 默认 file 来源
    ran: list[str] = []
    monkeypatch.setattr(win, "_run_task", lambda tid: ran.append(tid))
    win._start_task()

    assert win._pool_origin() == {"file"}
    assert len(win.store.list()) == 1              # 整池一个任务
    assert len(ran) == 1
    win.close()


def test_start_task_single_dir_stays_one_task(app, config, tmp_path, monkeypatch):
    from taggycat.ui.main_window import MainWindow

    files = _mk_files(tmp_path, "solo/ep01.mp4", "solo/ep02.mp4")
    win = MainWindow(config)
    win._append_pool(files, origin="dir")
    ran: list[str] = []
    monkeypatch.setattr(win, "_run_task", lambda tid: ran.append(tid))
    win._start_task()

    assert len(win.store.list()) == 1              # 单目录不拆
    assert len(ran) == 1
    assert win.auto_queue == []
    win.close()


# ---------------------------------------------------------------------
# 主窗口：过滤 / 管理操作 / 队列
# ---------------------------------------------------------------------
def test_task_filter_search_and_status(app, config, tmp_path):
    from taggycat.store import TaskStore
    from taggycat.ui.main_window import MainWindow

    store = TaskStore(config)
    t1 = _mk_task(store, _mk_files(tmp_path, "a.mp4"), name="好看的电影")
    t2 = _mk_task(store, _mk_files(tmp_path, "b.mp4"), name="普通电影")

    win = MainWindow(config)
    win._refresh_tasks(force=True)
    assert win.task_table.rowCount() == 2

    win.ed_filter.setText("好看")                  # textChanged → 200ms 防抖
    win.search_timer.timeout.emit()
    assert win.task_table.rowCount() == 1
    assert win.task_table.item(0, 0).text() == "好看的电影"
    assert win.task_table.item(0, 0).data(0x0100) == t1.task_id  # Qt.UserRole

    win.ed_filter.clear()
    store.set_archived(t2.task_id, True)
    win._refresh_tasks(force=True)
    assert win.task_table.rowCount() == 1          # “全部”隐藏归档

    win.cb_status_filter.setCurrentText("已归档")
    assert win.task_table.rowCount() == 1
    assert win.task_table.item(0, 0).text() == "普通电影"
    assert win.task_table.item(0, 0).data(0x0100) == t2.task_id  # Qt.UserRole
    win.close()


def test_task_manage_handlers(app, config, tmp_path, monkeypatch):
    from taggycat.store import TaskStore
    from taggycat.ui import main_window as MW

    store = TaskStore(config)
    st = _mk_task(store, _mk_files(tmp_path, "m.mp4"), name="旧名")

    win = MW.MainWindow(config)
    monkeypatch.setattr(
        MW.QInputDialog, "getText",
        staticmethod(lambda *a, **k: ("改好了", True)),
    )
    win._rename_task(st.task_id, "旧名")
    assert store.load(st.task_id).name == "改好了"

    win._toggle_archive(st.task_id, True)
    assert store.load(st.task_id).archived is True

    monkeypatch.setattr(
        MW.QMessageBox, "question",
        staticmethod(lambda *a, **k: MW.QMessageBox.StandardButton.Yes),
    )
    win._delete_task(st.task_id, "改好了")
    dw = next(iter(win.findChildren(MW.DeleteWorker)))
    dw.wait(5000)
    app.processEvents()
    assert not store.exists(st.task_id)
    win.close()


def test_delete_runs_off_main_thread(app, config, tmp_path, monkeypatch):
    """TR-18.1: rmtree 在非主线程执行，完成回调刷新表格；失败经槽兜底弹窗。"""
    import threading
    import time

    from taggycat.store import TaskStore
    from taggycat.ui import main_window as MW
    from taggycat.ui.workers import DeleteWorker

    store = TaskStore(config)
    win = MW.MainWindow(config)
    store = win.store                          # 与窗口同一个 store
    st = _mk_task(store, _mk_files(tmp_path, "d.mp4"), name="删掉")

    monkeypatch.setattr(
        MW.QMessageBox, "question",
        staticmethod(lambda *a, **k: MW.QMessageBox.StandardButton.Yes),
    )
    real_delete = store.delete
    idents: list[int | None] = []

    def slow_delete(tid):
        idents.append(threading.current_thread().ident)
        time.sleep(0.15)
        real_delete(tid)

    monkeypatch.setattr(store, "delete", slow_delete)

    win._delete_task(st.task_id, "删掉")
    assert "正在删除" in win.statusBar().currentMessage()
    dw = next(iter(win.findChildren(DeleteWorker)))
    dw.wait(5000)
    app.processEvents()

    assert idents and idents[0] != threading.main_thread().ident
    assert not store.exists(st.task_id)
    assert win.statusBar().currentMessage() == ""

    # 失败路径：worker 发 failed，槽内 QMessageBox 兜底
    boxes: list[str] = []
    monkeypatch.setattr(
        MW.QMessageBox, "warning",
        staticmethod(lambda *a, **k: boxes.append(a[2])),
    )
    st2 = _mk_task(store, _mk_files(tmp_path, "f.mp4"), name="失败")

    def fail_delete(tid):
        raise OSError("占用中")

    dw2 = DeleteWorker(store, st2.task_id)
    monkeypatch.setattr(store, "delete", fail_delete)
    dw2.failed.connect(win._on_task_delete_failed)
    dw2.start()
    dw2.wait(5000)
    app.processEvents()
    assert boxes and "占用中" in boxes[0]
    assert store.exists(st2.task_id)
    win.close()


def test_delete_cancel_keeps_task(app, config, tmp_path, monkeypatch):
    from taggycat.store import TaskStore
    from taggycat.ui import main_window as MW

    store = TaskStore(config)
    st = _mk_task(store, _mk_files(tmp_path, "k.mp4"), name="别删我")

    win = MW.MainWindow(config)
    monkeypatch.setattr(
        MW.QMessageBox, "question",
        staticmethod(lambda *a, **k: MW.QMessageBox.StandardButton.No),
    )
    win._delete_task(st.task_id, "别删我")
    assert store.exists(st.task_id)
    win.close()


def test_queue_advance_and_stop(app, config, tmp_path, monkeypatch):
    from taggycat.store import TaskStore
    from taggycat.ui.main_window import MainWindow

    store = TaskStore(config)
    f = _mk_files(tmp_path, "q.mp4")
    t1 = _mk_task(store, f, name="队列1")
    t2 = _mk_task(store, f, name="队列2")

    win = MainWindow(config)
    ran: list[str] = []
    monkeypatch.setattr(win, "_run_task", lambda tid: ran.append(tid))

    win.auto_queue = [t1.task_id, t2.task_id]
    win._advance_queue()
    assert ran == [t1.task_id]                     # 启动首个
    assert win.auto_queue == [t2.task_id]          # 剩余排队

    win._on_completed(t1.task_id)                  # 完成 → 自动开下一个
    assert ran == [t1.task_id, t2.task_id]
    assert win.auto_queue == []

    win.auto_queue = [t1.task_id]
    win._stop_task()                               # 停止 → 中断队列
    assert win.auto_queue == []
    win.close()


def test_queue_skips_missing_and_finished(app, config, tmp_path, monkeypatch):
    from taggycat.models import StageStatus
    from taggycat.store import TaskStore
    from taggycat.ui.main_window import MainWindow

    store = TaskStore(config)
    f = _mk_files(tmp_path, "z.mp4")
    gone = _mk_task(store, f, name="将被删除")
    done_t = _mk_task(store, f, name="已完成")
    fresh = _mk_task(store, f, name="新任务")

    store.delete(gone.task_id)
    state = store.load(done_t.task_id)
    state.finished = True
    for s in state.stages.values():
        s.status = StageStatus.DONE
    store.save(state)

    win = MainWindow(config)
    ran: list[str] = []
    monkeypatch.setattr(win, "_run_task", lambda tid: ran.append(tid))
    win.auto_queue = [gone.task_id, done_t.task_id, fresh.task_id]
    win._advance_queue()
    assert ran == [fresh.task_id]                  # 跳过不存在/已完成
    win.close()


def test_entry_points_present(app, config):
    from taggycat.ui.main_window import MainWindow

    win = MainWindow(config)
    acts = [a for m in win.menuBar().actions() for a in m.menu().actions()]
    assert any("接口设置" in a.text() for a in acts)
    texts = [b.text() for b in win.findChildren(QPushButton)]
    assert "接口设置…" in texts
    assert "批量队列…" in texts
    win.close()
