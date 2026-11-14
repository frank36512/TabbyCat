@echo off
chcp 65001 >nul
rem =============================================================
rem  虎猫解说 (taggycat) 一键构建单文件 exe
rem  产物: dist\taggycat.exe（无需 Python 环境即可运行）
rem =============================================================
setlocal
cd /d "%~dp0"

echo ============================================================
echo  虎猫解说 (taggycat) - 构建单文件 exe
echo ============================================================

where uv >nul 2>nul
if errorlevel 1 goto :nouv

echo [1/3] 同步依赖（GUI / LLM / ASR / TTS / PyInstaller，首次较慢）...
uv sync --all-extras
if errorlevel 1 goto :failed

echo [2/3] 清理旧构建产物...
if exist build rd /s /q build
if exist dist\taggycat.exe del /f /q dist\taggycat.exe

echo [3/3] PyInstaller 打包中（数分钟，取决于机器性能）...
uv run pyinstaller --noconfirm --clean taggycat.spec
if errorlevel 1 goto :failed

echo.
echo [完成] %~dp0dist\taggycat.exe
echo   单文件可直接分发；首次启动会在 exe 同目录生成 config.toml。
echo   记得把 exe 放在可写目录（如桌面、D 盘根目录），便于保存配置与产物。
dir dist\taggycat.exe
pause
exit /b 0

:failed
echo.
echo [错误] 构建失败。请查看上方 PyInstaller 输出；
echo        常见原因：未安装 Python 3.12 / FFmpeg 缺失 / 依赖下载中断。
pause
exit /b 1

:nouv
echo [错误] 未找到 uv，请先安装: https://docs.astral.sh/uv/
echo        安装命令: powershell -c "irm https://astral.sh/uv/install.ps1 | iex"
pause
exit /b 1
