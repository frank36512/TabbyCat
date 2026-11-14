"""路径解析统一入口：源码运行与 PyInstaller onefile 行为一致。

单文件 exe 把随包只读资源解压到 ``sys._MEIPASS``，而配置、任务产物仍位于
exe 所在目录。当相对路径指向 ``resources/`` 且在配置目录找不到时，
自动回退到 bundle 根，避免用户必须额外携带一个 resources 文件夹。
"""

from __future__ import annotations

import sys
from pathlib import Path


def bundle_root() -> Path | None:
    """PyInstaller 运行时解压目录；源码运行时返回 None。"""
    base = getattr(sys, "_MEIPASS", None)
    return Path(base) if base else None


def resolve_path(p: str | Path, root: str | Path | None = None) -> Path:
    """解析路径：绝对路径原样返回；相对路径基于 root（默认 CWD）。

    仅对 ``resources/`` 下的随包资源做 bundle 回退；workspace/、output/ 等
    运行时产物始终写到 root 下，不会被误导向临时解压目录。
    """
    path = Path(p)
    if path.is_absolute():
        return path
    base = Path(root) if root is not None else Path.cwd()
    candidate = (base / path).resolve()
    if not candidate.exists() and path.parts and path.parts[0] == "resources":
        bundle = bundle_root()
        if bundle:
            bundled = (bundle / path).resolve()
            if bundled.exists():
                return bundled
    return candidate
