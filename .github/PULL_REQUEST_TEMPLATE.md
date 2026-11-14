## 这个 PR 做了什么

<!-- 一句话说明改动，附上相关 Issue 编号 -->

## 改动类型

- [ ] 功能（feat）
- [ ] 修复（fix）
- [ ] 文档（docs）
- [ ] 重构 / 性能（refactor）
- [ ] 测试（test）
- [ ] 构建 / 发布（chore）

## 检查清单

- [ ] `uv run ruff check taggycat tests` 通过
- [ ] `uv run pytest tests -q` 通过
- [ ] 新增逻辑补充了测试；LLM / TTS / ASR 调用已 monkeypatch，不依赖真实接口
- [ ] 涉及配置字段时，同步更新 `config.example.toml` 与 `taggycat/config.example.toml`
- [ ] 涉及界面时，附截图或说明（UI 测试可在 offscreen 下运行）
- [ ] 未提交 `config.toml`、API Key、内网地址或任何私有素材
- [ ] 第三方素材已在 `NOTICE` 中登记，允许按 Apache-2.0 分发
- [ ] 涉及版本号时，同步更新 `taggycat/__init__.py`、`pyproject.toml`、`packaging/file_version_info.txt`、`CHANGELOG.md`

## 附加说明

<!-- 复现步骤、性能影响、兼容性影响、遗留问题 -->
