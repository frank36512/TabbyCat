# 更新日志

本项目遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 与语义化版本。

## [0.1.0] - 2026-11-14

首个开源准备版本。

### 新增

- 品牌统一：中文名「虎猫解说」、英文名 `taggycat`；桌面窗口、关于框、CLI、自检报告、配置模板、文档全部改用新名称。
- 品牌图标 `taggycat.png` / `taggycat.ico`（7 种尺寸、透明背景），`scripts/render_icon.py` 可从主图一键重建全套图标。
- 单文件 exe 打包配置 `taggycat.spec`（PyInstaller onefile）、`build_exe.bat`、Windows 版本资源 `packaging/file_version_info.txt`。
- onefile 运行时适配：无参数双击默认启动 GUI；首次启动在 exe 同目录生成 `config.toml`；`resources/` 资源可从 bundle 回退解析。
- 开源发布材料：Apache-2.0 `LICENSE`、`NOTICE`、`CONTRIBUTING.md`、`SECURITY.md`、Issue/PR 模板、GitHub Actions 的 CI 与 Release 工作流。
- 示例配置脱敏：API Key 统一占位符、内网地址改为 `127.0.0.1`。

### 变更

- Python 包与 CLI 由 `clipnarra` 重命名为 `taggycat`；环境变量改为 `TAGGYCAT_CONFIG`（仍兼容旧的 `CLIPNARRA_CONFIG`，会提示弃用）。
- 项目元数据补充 readme、许可、作者、关键词与 classifiers；`pyinstaller` 纳入开发依赖。

### 迁移提示

- 源码/脚本调用：把 `uv run clipnarra ...` 改为 `uv run taggycat ...`。
- Python 导入：把 `from clipnarra...` 改为 `from taggycat...`。
- 环境变量：`CLIPNARRA_CONFIG` → `TAGGYCAT_CONFIG`。
- 已有 `workspace/` 任务数据与 `config.toml` 保持不变，可直接继续使用。

发布后请在此补充该版本 Release 链接。
