# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 单文件（onefile）打包配置：虎猫解说 (taggycat)。

用法：
    uv sync --all-extras
    uv run pyinstaller --noconfirm --clean taggycat.spec
   或直接双击 build_exe.bat

产物：
    dist/taggycat.exe —— 单个可执行文件，内含 Python 运行时、PyQt6、
    resources/ 图标与工作流、配置模板；图标为根目录 taggycat.ico。

说明：
    - console=False：GUI 应用，不弹黑色控制台窗口
    - upx=False：避免 UPX 壳导致杀毒软件误报
    - onefile 启动时资源解压到 sys._MEIPASS，代码里 config.resolve() /
      find_config() / ui._resource_root() 已做相应适配
    - 首次启动会在 exe 同目录生成 config.toml（从随包模板复制）
"""

from pathlib import Path

ROOT = Path(SPECPATH)

a = Analysis(
    ["taggycat/__main__.py"],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[
        # 运行时窗口 / 任务栏图标
        ("resources/icons", "resources/icons"),
        # ComfyUI 工作流（API 格式 JSON）
        ("resources/workflows", "resources/workflows"),
        # 配置模板：根目录副本供首次启动生成 config.toml，包内副本供 `init` 命令
        ("config.example.toml", "."),
        ("taggycat/config.example.toml", "taggycat"),
        # 品牌资源（便于用户从 bundle 取用）
        ("taggycat.ico", "."),
        ("taggycat.png", "."),
    ],
    hiddenimports=[
        "PyQt6.QtMultimedia",
        "PyQt6.QtMultimediaWidgets",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "tkinter",
        "pytest",
        "matplotlib",
    ],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="taggycat",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ROOT / "taggycat.ico"),
    version=str(ROOT / "packaging" / "file_version_info.txt"),
)
