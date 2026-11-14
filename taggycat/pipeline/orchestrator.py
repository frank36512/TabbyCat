"""流水线编排器：阶段状态机 + 断点续跑。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from loguru import logger

from taggycat.config import AppConfig
from taggycat.models import STAGE_ORDER, StageName, StageStatus, TaskState
from taggycat.store import TaskStore


class StageError(RuntimeError):
    """阶段执行失败。"""


class ReviewRequired(RuntimeError):
    """校验文案开启：流水线暂停等待人工审核（非失败）。"""


@dataclass
class StageContext:
    """阶段执行上下文。"""

    config: AppConfig
    store: TaskStore
    state: TaskState

    @property
    def task_dir(self):
        return self.store.task_dir(self.state.task_id)

    def stage_dir(self, stage: str | StageName):
        key = stage.value if isinstance(stage, StageName) else stage
        return self.store.stage_dir(self.state.task_id, key)

    @property
    def source_paths(self) -> list[str]:
        return [s.path for s in self.state.sources]


StageFunc = Callable[[StageContext], dict]


@dataclass
class StageSpec:
    name: StageName
    func: StageFunc | None = None      # None = 尚未实现
    description: str = ""


class Pipeline:
    """按 STAGE_ORDER 执行阶段，逐阶段落盘 state.json。"""

    def __init__(self, config: AppConfig, store: TaskStore | None = None):
        self.config = config
        self.store = store or TaskStore(config)
        self._stages: dict[str, StageSpec] = {}

    # ---------- 注册 ----------
    def register(self, spec: StageSpec) -> Pipeline:
        self._stages[spec.name.value] = spec
        return self

    def get(self, name: StageName | str) -> StageSpec:
        key = name.value if isinstance(name, StageName) else name
        if key not in self._stages:
            raise KeyError(f"未知阶段: {key}")
        return self._stages[key]

    @property
    def stage_specs(self) -> list[StageSpec]:
        return [self.get(s) for s in STAGE_ORDER]

    # ---------- 执行 ----------
    def run(
        self,
        task_id: str,
        *,
        only_stage: str | None = None,
        force: bool = False,
        stop_on_unimplemented: bool = True,
        until: str | None = None,
        on_stage: Callable[[str, str], None] | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> TaskState:
        """执行任务流水线。

        - only_stage: 只跑指定阶段
        - force: 已完成的阶段强制重跑
        - stop_on_unimplemented: 遇到未实现阶段即停止（M1~M4 渐进放开）
        - until: 执行到该阶段（含）后停止，不调用其后的阶段
        - on_stage: 进度回调 (stage_key, event)，event ∈ start|done|fail|review|skip
        - should_stop: 每个阶段开始前调用，返回 True 则协作式取消
        """
        state = self.store.load(task_id)
        ctx = StageContext(config=self.config, store=self.store, state=state)

        def _emit(stage_key: str, event: str) -> None:
            if on_stage:
                try:
                    on_stage(stage_key, event)
                except Exception:  # noqa: BLE001
                    logger.debug("on_stage 回调异常", exc_info=True)

        if only_stage:
            order = [only_stage]
            self.get(only_stage)   # 校验存在
        else:
            order = [s.value for s in STAGE_ORDER]
            if until:
                self.get(until)    # 校验存在
                order = order[: order.index(until) + 1]

        for stage_key in order:
            if isinstance(stage_key, StageName):
                stage_key = stage_key.value
            spec = self.get(stage_key)
            st = state.stage(stage_key)

            if should_stop and should_stop():
                logger.warning("收到取消请求，停在 {} 之前", stage_key)
                state.current_stage = stage_key
                self.store.save(state)
                break

            if not force and st.status == StageStatus.DONE and not only_stage:
                logger.info("[{}] 已完成，跳过（--force 可重跑）", stage_key)
                _emit(stage_key, "skip")
                continue

            if spec.func is None:
                msg = f"阶段 `{stage_key}` 尚未实现（后续里程碑交付）"
                if stop_on_unimplemented and not only_stage:
                    logger.warning("{}", msg)
                    st.status = StageStatus.PENDING
                    state.current_stage = stage_key
                    self.store.save(state)
                    break
                st.status = StageStatus.SKIPPED
                st.error = msg
                self.store.save(state)
                _emit(stage_key, "skip")
                continue

            # ---- 执行 ----
            st.status = StageStatus.RUNNING
            st.started_at = datetime.now().isoformat(timespec="seconds")
            st.error = None
            state.current_stage = stage_key
            self.store.save(state)
            logger.info("[{}] 开始执行 ...", stage_key)
            _emit(stage_key, "start")

            try:
                outputs = spec.func(ctx) or {}
            except ReviewRequired as e:
                # 人工审核关卡：优雅暂停，非失败
                st.status = StageStatus.REVIEW
                st.finished_at = datetime.now().isoformat(timespec="seconds")
                st.error = str(e)
                self.store.save(state)
                logger.warning("[{}] 等待人工审核：{}", stage_key, str(e).replace("\n", " "))
                _emit(stage_key, "review")
                break
            except Exception as e:  # noqa: BLE001
                st.status = StageStatus.FAILED
                st.finished_at = datetime.now().isoformat(timespec="seconds")
                st.error = str(e)
                self.store.save(state)
                logger.exception("[{}] 失败: {}", stage_key, e)
                _emit(stage_key, "fail")
                raise StageError(f"阶段 {stage_key} 失败: {e}") from e

            st.status = StageStatus.DONE
            st.finished_at = datetime.now().isoformat(timespec="seconds")
            st.outputs.update({k: str(v) for k, v in outputs.items()})
            self.store.save(state)
            logger.info("[{}] 完成 ✔", stage_key)
            _emit(stage_key, "done")

        # 检查整体完成
        if all(self.store.load(task_id).stage(s).status == StageStatus.DONE for s in STAGE_ORDER):
            state = self.store.load(task_id)
            state.finished = True
            state.current_stage = None
            self.store.save(state)
        return self.store.load(task_id)
