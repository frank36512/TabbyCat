"""pytest 全局配置：Windows 控制台 UTF-8 输出。"""

import contextlib
import sys

for stream in (sys.stdout, sys.stderr, sys.stdin):
    with contextlib.suppress(Exception):
        stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
