# 参与贡献

欢迎参与虎猫解说 (taggycat) 的开发。项目使用 Python 3.12+、[uv](https://docs.astral.sh/uv/) 管理依赖，桌面界面基于 PyQt6。

## 本地开发环境

```powershell
git clone <你的仓库地址>
cd taggycat
uv sync --all-extras          # 安装运行 + 开发依赖（含 PyInstaller）
uv run taggycat init          # 生成 config.toml
uv run taggycat doctor        # 自检 FFmpeg / 接口 / 字体
uv run taggycat ui            # 启动界面
```

> 请勿提交 `config.toml`：它含你的 API Key 与内网地址，已在 `.gitignore` 中排除。
> 修改配置示例时，只改 `config.example.toml` 与 `taggycat/config.example.toml`，
> 两者需保持一致，且只能使用占位符（如 `sk-REPLACE_ME`）与 `127.0.0.1`。

## 提交前检查

```powershell
uv run ruff check taggycat tests
uv run pytest tests -q
```

- 新增/修改解说风格时，同步更新 `taggycat/prompts.py` 的 `STYLE_PRESETS` 与 `_STYLE_EXTRAS`；`tests/test_prompts.py` 会校验字段完整性与风格差异。
- ASR / VLM / LLM / TTS 相关改动必须通过 monkeypatch 写测试，不要在 CI 中调用真实付费接口。
- 界面改动请在 `QT_QPA_PLATFORM=offscreen` 下跑 UI 测试。
- 修改图标源文件 `taggycat.png` 后，执行 `python scripts/render_icon.py` 重新生成 `taggycat.ico` 与 `resources/icons/app_*.png`，并把生成物一并提交。

## 打包单文件 exe

```powershell
build_exe.bat
# 或
uv run pyinstaller --noconfirm --clean taggycat.spec
```

产物为 `dist/taggycat.exe`。打包适配逻辑位于 `taggycat.spec` 与 `packaging/file_version_info.txt`；运行时路径处理见 `taggycat/config.py` 的 `find_config()` / `AppConfig.resolve()` 与 `taggycat/ui/main_window.py` 的 `_resource_root()`。

## 提交与 PR

- 提交信息建议：`类型: 简述`，如 `fix: 修复批量队列停止后状态残留`；类型用 `feat / fix / docs / refactor / test / chore`。
- 一个 PR 聚焦一件事，附复现步骤或效果截图（界面改动）。
- 在 PR 描述中说明是否影响配置字段、是否涉及 API Key、是否更新了文档与测试。
- 首次贡献请确认你提交的代码与素材允许按 Apache-2.0 再授权；第三方素材需在 `NOTICE` 中登记。

## 发布新版本

1. 更新 `taggycat/__init__.py` 的 `__version__`、`pyproject.toml` 的 `version`、`packaging/file_version_info.txt` 的 `filevers/prodvers` 与版本字符串。
2. 更新 `CHANGELOG.md`，本地跑 `build_exe.bat` 冒烟测试（启动界面、生成 config.toml）。
3. 打标签 `git tag v0.1.1 && git push origin v0.1.1`，`.github/workflows/release.yml` 会自动构建 exe、生成 SHA256 并发布 Release。
