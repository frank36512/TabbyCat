# 安全政策

## 支持版本

| 版本 | 支持 |
|------|------|
| 0.1.x | ✅ |

## 报告漏洞

**请勿在公开 Issue 中披露安全漏洞。** 请通过仓库的 Security Advisories（ privately report a vulnerability ）提交，或邮件联系维护者（发布前请在仓库设置中补充安全邮箱）。

报告请包含：影响版本、复现步骤、潜在影响、建议修复方案（如有）。我们会在确认后于修复版本中致谢。

## 使用者的密钥安全

虎猫解说会调用云端 LLM / VLM 与本地 ComfyUI 服务，请务必注意：

- `config.toml` 含 API Key 与内网地址，已被 `.gitignore` 排除，**不要提交、不要随 exe 一起分发**。
- 分发单文件 exe 时只分发 `dist/taggycat.exe`；exe 首次启动会在同目录生成一份不含真实密钥的 `config.toml` 模板。
- 示例配置 `config.example.toml` 只允许出现占位符；提交前请自查 `git diff` 中是否混入 `sk-` 开头的真实密钥或 `192.168.*` 等内网地址。
- 界面「设置 → 接口设置」会把填写的 Key 写回 `config.toml`，共享屏幕或录屏前请注意遮挡。
- ComfyUI / Ollama 若监听局域网地址，请自行做好网络隔离；项目不对这些外部服务做鉴权。

## 依赖与供应链

- 依赖版本由 `pyproject.toml` 约束，`uv.lock` 应提交以保证可复现。
- 单文件 exe 由 GitHub Actions 在干净的 `windows-latest` 环境构建，并附带 SHA256 校验值；请从 Release 页面下载并核对。
- PyInstaller onefile 产物偶尔会被杀毒软件误报（未使用 UPX 加壳以降低概率），请以 Release 的 SHA256 为准。
