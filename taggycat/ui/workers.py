"""流水线后台线程：执行任务并把进度发回 UI。"""

from __future__ import annotations

import threading

from loguru import logger
from PyQt6.QtCore import QThread, pyqtSignal

from taggycat.config import AppConfig
from taggycat.models import STAGE_ORDER, StageStatus
from taggycat.pipeline import StageError, build_pipeline


class PipelineWorker(QThread):
    """在后台线程执行一个任务的流水线。"""

    stage_event = pyqtSignal(str, str)          # (stage, event: start|done|fail|review|skip)
    log = pyqtSignal(str)
    failed = pyqtSignal(str)                    # 错误信息
    review_required = pyqtSignal(str)           # task_id
    completed = pyqtSignal(str)                 # task_id

    def __init__(self, config: AppConfig, task_id: str, parent=None):
        super().__init__(parent)
        self.config = config
        self.task_id = task_id
        self._stop = threading.Event()

    def stop(self) -> None:
        """协作式取消：当前阶段结束后停止。"""
        self._stop.set()

    def _on_stage(self, stage: str, event: str) -> None:
        self.stage_event.emit(stage, event)
        if event == "review":
            self.review_required.emit(self.task_id)

    def run(self) -> None:  # noqa: D401
        # 注意：未捕获异常逃出 QThread.run 会让 PyQt6 直接 abort 进程，必须全兜底
        try:
            p = build_pipeline(self.config)
            state = p.run(
                self.task_id,
                on_stage=self._on_stage,
                should_stop=self._stop.is_set,
            )
        except StageError as e:
            self.failed.emit(str(e))
            return
        except Exception as e:  # noqa: BLE001
            logger.exception("任务执行异常: {}", e)
            self.failed.emit(f"内部错误: {e}")
            return

        if self._stop.is_set():
            self.log.emit("已停止（可稍后继续运行）")
            self.failed.emit("已取消")
            return

        review = state.stage("review")
        if review.status == StageStatus.REVIEW:
            return  # 等待人工审核，不发 completed

        done = sum(
            1 for s in STAGE_ORDER
            if state.stage(s).status == StageStatus.DONE
        )
        if state.finished:
            self.log.emit(f"任务完成 {self.task_id}（{done}/{len(STAGE_ORDER)} 阶段）")
            self.completed.emit(self.task_id)
        elif done < len(STAGE_ORDER):
            self.failed.emit(f"停在 {state.current_stage or '?'}")


class PreviewWorker(QThread):
    """试听样音合成（后台线程，避免阻塞 GUI）。"""

    done = pyqtSignal(str)      # 音频路径
    failed = pyqtSignal(str)    # 错误信息

    def __init__(self, config: AppConfig, text: str, voice: str = "",
                 engine: str = "", parent=None):
        super().__init__(parent)
        self.config = config
        self.text = text
        self.voice = voice
        self.engine = engine

    def run(self) -> None:
        try:
            cfg = self.config.model_copy(deep=True)
            if self.engine:
                cfg.tts.engine = self.engine
            from taggycat.tts import get_tts

            sample_dir = cfg.resolve(cfg.paths.voice_samples)
            sample_dir.mkdir(parents=True, exist_ok=True)
            engine_obj = get_tts(cfg)
            out = engine_obj.synthesize(
                self.text, sample_dir / f"preview_{engine_obj.name}",
                voice=self.voice or None,
            )
            self.done.emit(str(out))
        except Exception as e:  # noqa: BLE001
            logger.exception("试听失败: {}", e)
            self.failed.emit(str(e))


class ProbeWorker(QThread):
    """接口探针（后台线程，避免测试连接阻塞 GUI）。"""

    succeeded = pyqtSignal(str)
    failed = pyqtSignal(str)

    def __init__(self, fn, parent=None):
        super().__init__(parent)
        self._fn = fn

    def run(self) -> None:
        try:
            self.succeeded.emit(self._fn())
        except Exception as e:  # noqa: BLE001
            self.failed.emit(str(e))


class DeleteWorker(QThread):
    """任务目录删除（后台线程，避免大目录 rmtree 阻塞 UI）。"""

    succeeded = pyqtSignal(str)             # task_id
    failed = pyqtSignal(str, str)           # task_id, error

    def __init__(self, store, task_id: str, parent=None):
        super().__init__(parent)
        self._store = store
        self._task_id = task_id

    def run(self) -> None:
        try:
            self._store.delete(self._task_id)
        except Exception as e:  # noqa: BLE001
            self.failed.emit(self._task_id, str(e))
            return
        self.succeeded.emit(self._task_id)
