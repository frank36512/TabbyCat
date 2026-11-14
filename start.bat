@echo off
chcp 65001 >nul
rem =============================================================
rem  虎猫解说 (taggycat) 启动脚本（双击运行）
rem  用法: start.bat           启动桌面界面
rem        start.bat doctor    只做环境自检后退出
rem =============================================================
setlocal
cd /d "%~dp0"

where uv >nul 2>nul
if errorlevel 1 goto :nouv

if /i "%~1"=="doctor" goto :doctor

echo 正在启动 虎猫解说 (taggycat) ...
uv run taggycat ui
if errorlevel 1 goto :failed
goto :eof

:doctor
uv run taggycat doctor
set RC=%errorlevel%
echo.
pause
exit /b %RC%

:failed
echo.
echo [提示] 启动失败。请自检: start.bat doctor
echo        首次使用请先执行 uv sync --all-extras
pause
exit /b 1

:nouv
echo [错误] 未找到 uv，请先安装: https://docs.astral.sh/uv/
echo        安装命令: powershell -c "irm https://astral.sh/uv/install.ps1 | iex"
pause
exit /b 1
