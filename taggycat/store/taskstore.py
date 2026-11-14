"""任务存储：workspace/<task_id>/ 产物落盘与断点状态。"""

from __future__ import annotations

import json
import os
import re
import shutil
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

from loguru import logger

from taggycat.config import AppConfig
from taggycat.models import (
    STAGE_ORDER,
    ProjectConfig,
    SourceFile,
    StageState,
    TaskState,
)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _slugify(name: str) -> str:
    s = re.sub(r"[^\w\u4e00-\u9fff-]+", "_", name).strip("_")
    return s or "task"


class TaskStore:
    """任务工作区管理（创建/读写/列出）。"""

    def __init__(self, config: AppConfig):
        self.config = config
        self.root = config.workspace_dir / "tasks"
        self.root.mkdir(parents=True, exist_ok=True)

    # ---------- 路径 ----------
    def task_dir(self, task_id: str) -> Path:
        return self.root / task_id

    def stage_dir(self, task_id: str, stage: str) -> Path:
        d = self.task_dir(task_id) / stage
        d.mkdir(parents=True, exist_ok=True)
        return d

    def state_path(self, task_id: str) -> Path:
        return self.task_dir(task_id) / "state.json"

    # ---------- 创建 ----------
    def create(
        self,
        sources: list[str | Path],
        *,
        name: str = "",
        project: ProjectConfig | None = None,
    ) -> TaskState:
        paths = [Path(s).expanduser().resolve() for s in sources]
        missing = [str(p) for p in paths if not p.exists()]
        if missing:
            raise FileNotFoundError(f"素材不存在: {', '.join(missing)}")
        if not paths:
            raise ValueError("素材池为空")

        project = project or ProjectConfig()
        ts = time.strftime("%Y%m%d-%H%M%S")
        slug = _slugify(name or paths[0].stem)
        task_id = f"{ts}_{slug}_{uuid.uuid4().hex[:6]}"
        if not name:
            name = paths[0].stem

        src_files = []
        for p in paths:
            src_files.append(
                SourceFile(
                    path=str(p),
                    name=p.name,
                    size_mb=round(p.stat().st_size / (1024 * 1024), 2),
                    media_id=_slugify(p.stem)[:40],
                )
            )

        state = TaskState(
            task_id=task_id,
            name=name,
            created_at=_now(),
            updated_at=_now(),
            project=project,
            sources=src_files,
            stages={s.value: StageState(name=s.value) for s in STAGE_ORDER},
        )
        self.task_dir(task_id).mkdir(parents=True, exist_ok=True)
        self.save(state)
        logger.info("任务已创建: {} ({} 个素材)", task_id, len(src_files))
        return state

    # ---------- 读写 ----------
    # Windows: 读句柄（GUI 刷新/杀毒扫描）默认不带 FILE_SHARE_DELETE，
    # 会让 os.replace 报 WinError 5（拒绝访问）→ 短暂占用需重试。
    _REPLACE_RETRIES = 10
    _REPLACE_DELAY = 0.1

    def save(self, state: TaskState) -> None:
        state.updated_at = _now()
        path = self.state_path(state.task_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            state.model_dump(mode="json"), ensure_ascii=False, indent=2
        )
        # 唯一 tmp 名：并发保存时不互踩
        tmp = path.parent / (
            f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
        )
        tmp.write_text(payload, encoding="utf-8")

        last: OSError | None = None
        for i in range(max(1, self._REPLACE_RETRIES)):
            try:
                tmp.replace(path)   # 原子替换
                return
            except PermissionError as e:
                last = e
                time.sleep(self._REPLACE_DELAY * (i + 1))

        # 仍被占用 → 回退直写（读共享允许写入，仅 replace 需要删除权限）
        try:
            path.write_text(payload, encoding="utf-8")
            tmp.unlink(missing_ok=True)
            logger.warning(
                "state.json 被占用，原子替换重试失败，已回退直写: {}", path
            )
            return
        except OSError as e:
            tmp.unlink(missing_ok=True)
            raise last or e from e

    def load(self, task_id: str) -> TaskState:
        path = self.state_path(task_id)
        if not path.exists():
            raise FileNotFoundError(f"任务不存在: {task_id}")
        data = json.loads(path.read_text(encoding="utf-8"))
        return TaskState.model_validate(data)

    def list(self) -> list[TaskState]:
        states: list[TaskState] = []
        if not self.root.exists():
            return states
        for d in sorted(self.root.iterdir()):
            if (d / "state.json").exists():
                try:
                    states.append(self.load(d.name))
                except Exception as e:  # noqa: BLE001
                    logger.warning("加载任务失败 {}: {}", d.name, e)
        states.sort(key=lambda s: s.created_at, reverse=True)
        return states

    def exists(self, task_id: str) -> bool:
        return self.state_path(task_id).exists()

    # ---------- 任务管理 ----------
    def rename(self, task_id: str, name: str) -> TaskState:
        """重命名任务（同步更新展示名，不影响 task_id）。"""
        name = name.strip()
        if not name:
            raise ValueError("任务名不能为空")
        state = self.load(task_id)
        state.name = name
        self.save(state)
        logger.info("任务已重命名: {} -> {}", task_id, name)
        return state

    def set_archived(self, task_id: str, archived: bool) -> TaskState:
        """归档 / 取消归档（归档任务默认不出现在列表视图）。"""
        state = self.load(task_id)
        state.archived = archived
        self.save(state)
        logger.info("任务{}: {}", "已归档" if archived else "已取消归档", task_id)
        return state

    def delete(self, task_id: str, *, recursive: bool = True) -> None:
        """删除任务及其全部产物目录（不可恢复）。"""
        if not self.state_path(task_id).exists():
            raise FileNotFoundError(f"任务不存在: {task_id}")
        d = self.task_dir(task_id)
        if recursive:
            shutil.rmtree(d, ignore_errors=False)
        else:
            d.rmdir()
        logger.warning("任务已删除: {}（产物目录一并移除）", task_id)

    def resolve_task_id(self, key: str) -> str:
        """支持完整 ID / 时间戳前缀 / 名称模糊匹配。"""
        if self.exists(key):
            return key
        candidates = [s for s in self.list() if s.task_id.startswith(key) or s.name == key]
        if len(candidates) == 1:
            return candidates[0].task_id
        if not candidates:
            raise FileNotFoundError(f"任务不存在: {key}")
        raise ValueError(f"任务 ID 不唯一: {key} -> {[c.task_id for c in candidates]}")
