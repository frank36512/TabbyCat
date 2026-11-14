# 虎猫解说 (taggycat)

![Python](https://img.shields.io/badge/Python-3.12%2B-blue) ![Platform](https://img.shields.io/badge/Platform-Windows-blue) ![License](https://img.shields.io/badge/License-Apache%202.0-green)

一站式 **AI 影视解说 + 自动化剪辑** 本地系统：输入一批视频素材，自动完成
「台词识别 → 画面理解 → 解说文案 → 人工审核 → 配音 → 字幕 → 成片」全流程。

- 两种用法：直接双击单文件 `taggycat.exe`（无需 Python 环境），或源码运行（Python 3.12+ / uv / PyQt6）
- 运行环境：Windows + FFmpeg 8.x；ComfyUI / Ollama 为可选的本地推理引擎
- 开源许可：[Apache-2.0](LICENSE)，第三方与素材声明见 [NOTICE](NOTICE)
- 架构设计参考 [NarratoAI](https://github.com/linyqh/NarratoAI)（MIT），本项目代码为独立实现

## 功能

- 14 阶段流水线，每阶段产物落盘 `workspace/tasks/<task_id>/<stage>/`，可断点续跑、单阶段重跑
- 双轨理解：ASR 台词（字幕优先，faster-whisper 本地或 **ComfyUI 转录工作流** 兜底）
  + 抽帧 VLM 画面理解（云端 OpenAI 兼容 API 或 **本地 ComfyUI 视觉理解工作流**，可降级）
- LLM 文案：云端 OpenAI 兼容（DeepSeek/通义等）为主，本地 Ollama 备选；23 种解说风格预设
- **解说风格体系**：23 种风格不止是语气标签，每种固化了「开场钩子 / 叙述视角 / 节奏句长 /
  用词口头禅 / 常用手法 / 禁忌 / 选材偏好 / 推荐音色」，并以「风格卡」注入 analyze / plan /
  storyline / script 各阶段——同一段素材换风格，选材与文案都会真正不同（见「解说风格」）
- 配音三引擎：`edge`（在线）/ `comfyui`（本地工作流）/ `comfyui_clone`（语音克隆），带缓存
- 成片：横屏 16:9、逐段配音对齐、口播字幕烧录（**双语字幕可选**，LLM 翻译+缓存）、BGM 混音；
  **按目标时长出片**（plan 预算 = 目标时长 ±20%，不足目标 80% 自动从剩余候选补选）
- 音画同步：讲解段原片声音**静音**（`original_audio_mode = "mute"`）、台词密集段整段让路原声
  （`voice_yield`，无讲解时段原声+原字幕露出）；讲解段自动 **delogo 遮盖原片烧录字幕**
  （`hard_sub_remove` + `hard_sub_band` 遮盖带可调）；配音超长时片段出点**自动向后延伸**
  （受相邻片段/源边界钳制，消除画面定格）；口播字幕默认 64 号字
- 校验文案开关：开启 = 配音前人工审核（REVIEW 状态，GUI 弹窗或 CLI `task approve`）；
  关闭 = 字数不合格时自动 LLM 审核
- **接口设置对话框**：GUI 菜单「设置 → 接口设置」编辑 LLM/ASR/VLM/TTS 端点、Key、模型、
  工作流与语音配置（默认引擎/音色、配音缓存、试听），每项可一键测试连通，
  保存写回 config.toml（保留注释）
- **多主题风格**：菜单「设置 → 主题」实时切换 5 套主题（浅色·蓝/青绿/紫、深色·蓝黑/琥珀），
  原生对话框调色板同步跟随，选择写回 `[ui].theme`、重启沿用
- **任务管理**：任务表搜索/状态筛选，右键重命名、归档（默认列表隐藏）、删除（连产物）、
  「继续运行」「再次运行（失败重跑）」（失败/卡住任务一键复位重跑，已成功阶段保留续跑）
- **批量队列**：素材池一键拆成多个任务，默认拆分方式**按素材来源自动匹配**
  （「添加视频」= 每文件一个任务，「添加目录」= 按目录分组，对话框内可手动切换），
  可自动排队逐个执行，「停止」即中断队列（任务可随时右键「继续运行」）；
  两种素材添加方式**不可混用**（防误合并）；多目录池点「▶ 开始任务」= 自动按目录拆分
  成多任务并排队执行
- PyQt6 桌面界面（首行「基本信息 ∥ 节奏与输出」、中行「素材池 ∥ 任务队列」、底部操作按钮行；
  运行日志在「帮助 → 运行日志」，语音配置在「设置 → 接口设置」，主题风格在「设置 → 主题」；
  任务表右键可「继续运行」「审核文案」「再次运行」，重启后也能续跑历史任务；
  任务表「当前阶段」列显示阶段内细分进度，如 `understand 12/48帧`）

## 单文件 exe（推荐给普通用户）

从项目 Release 页面下载 `taggycat.exe`（或本地执行 `build_exe.bat` 生成 `dist/taggycat.exe`），
把它放进一个**可写目录**（桌面、`D:\taggycat` 等），直接双击即可：

1. 首次启动会在 exe 同目录生成 `config.toml`（不含真实密钥的模板）；
2. 在「设置 → 接口设置」填写 LLM / VLM 的 Base URL 与 API Key，或改走本地 Ollama / ComfyUI；
3. 任务产物写入同目录的 `workspace/` 与 `output/`。

> 单文件 exe 内含 Python 运行时、PyQt6、图标与工作流资源，首次启动需解压（约十几秒）。
> FFmpeg 仍需本机安装并加入 PATH（exe 不附带 FFmpeg）。被杀毒软件误报时请核对 Release 附带的
> `.sha256` 校验值。不要把自己填好 API Key 的 `config.toml` 随 exe 分发给别人。

构建命令与 CI 自动发布见 [CONTRIBUTING.md](CONTRIBUTING.md#打包单文件-exe) 与
`.github/workflows/release.yml`。

## 源码安装

```powershell
# 1) Python 3.12 + uv + FFmpeg（均已就绪的机器可跳过）
# 2) 安装全部依赖（含 llm/asr/tts/ui）
uv sync --all-extras

# 3) 生成配置并自检
uv run taggycat init
uv run taggycat doctor
```

> `uv sync --extra xxx` 会卸载其他 extras，请统一用 `--all-extras`。

## 快速开始

```powershell
# 1. 填配置：config.toml 的 [llm]/[vlm] base_url + api_key（或走 [llm_local] Ollama）
uv run taggycat doctor            # 自检 ffmpeg / LLM / TTS / 字体 / 目录

# 2. 创建任务（多文件素材池）
uv run taggycat task create D:\clips\a.mp4 D:\clips\b.mp4 `
    --name "我的解说" --style "电影解说" --target-duration 180 `
    --voice zh-CN-YunjianNeural --engine edge `
    --pov first --pov-character 男主          # 可选：沉浸式第一人称（默认跟随风格）

# 3. 执行（到人工审核处会停下）
uv run taggycat task run <task_id>

# 4. 审核文案（GUI 弹窗，或 CLI）
uv run taggycat task approve <task_id> [--file edited_segments.json]

# 5. 继续执行 → 生成成片（output/ 目录）
uv run taggycat task run <task_id>

# 桌面界面（一站式）
uv run taggycat ui
```

## 流水线（14 阶段）

| # | 阶段 | 说明 |
|---|------|------|
| 1 | `probe` | 媒体探测（时长/分辨率/音轨/字幕轨） |
| 2 | `subtitle` | 字幕准备（外挂/内嵌提取） |
| 3 | `asr` | 台词转录：字幕优先，faster-whisper 或 ComfyUI 工作流兜底 |
| 4 | `frames` | 场景切分 + 场景/均匀抽帧 |
| 5 | `understand` | VLM 画面理解 + 时间线构建（可降级纯台词；输出画面覆盖率，<50% 告警提示加帧） |
| 6 | `analyze` | LLM 剧情分析 |
| 7 | `plan` | 分段规划（按目标时长出片 + 素材过剩时按比例采样候选） |
| 8 | `script` | 解说文案生成（风格预设） |
| 9 | `match` | 素材匹配 + 字数校验与修复（±20%，约 4.5 字/秒） |
| 10 | `review` | 文案校验：人工审核 或 自动 LLM 审核 |
| 11 | `tts` | 逐段配音（edge / ComfyUI，带缓存与时长回填；配音超长片段自动延伸消除定格） |
| 12 | `subtitle_gen` | 口播字幕时间轴（单条 ≤18 字） |
| 13 | `assemble` | FFmpeg 逐段统一编码 + concat |
| 14 | `export` | BGM 混音 + 字幕烧录 + 导出成片 |

阶段间支持 `--until <stage>`（执行到某阶段停止）、`--stage <name> --force`（重跑单阶段）。

## 解说风格（23 种）

对标 NarratoAI / 解说猫 / AI 解说大师 等同类工具：**解说文案的质量七成取决于风格模板**。
虎猫解说 的每种风格都是一张结构化「风格卡」，而非一句形容词，包含：

| 维度 | 作用 | 注入阶段 |
|------|------|----------|
| 语气人设 / 叙述视角 | 谁在讲、用什么口吻 | analyze / storyline / script |
| 开场钩子 | 黄金三秒抓人手法（悬念式/反差式/提问式/结局前置/身份代入…） | storyline / script |
| 开场钩子模板库 | 该风格 2~3 条可直接化用的开场句式 | script（首段） |
| 结尾模板库 | 该风格 2~3 条观点升华/悬念收尾句式 | script（末段） |
| 节奏句长 / 语速 | 语速（字/秒）、长短句、留白与信息密度 | analyze / storyline / script / **tts** |
| 用词口头禅 / 常用手法 | 网感词、修辞与镜头细节点评 | script / repair |
| 选材偏好 | 该风格应挑哪类画面（悬疑重埋线、爽剧重反转、知识重例证…） | **plan** |
| 禁忌 | 该风格要避免的写法（报幕、剧透、术语轰炸…） | script |
| 推荐音色 | 该风格推荐的 edge 音色（GUI 选择风格时提示） | UI |

风格卡会随任务风格注入：`analyze` 侧重该风格的分析关注点、`plan` 据此挑选片段、
`storyline` 产出贴合风格的开场与结构、`script` 追加「开场钩子模板库 / 结尾模板库」、
风格示例句（few-shot）与用词手法要求。GUI「解说风格」下拉实时显示所选风格要点、语速与推荐音色。

- **风格专属语速**：每种风格有基准语速（如悬疑 4.0、短剧漫剧 5.4 字/秒），它同时驱动
  ①文案字数预算 ②edge 配音语速（换算为 edge rate，如 5.4 字/秒 → `+20%`），二者同源，
  保证「写多长」与「读多久」一致；ComfyUI 等语速不可控的引擎自动沿用基准，避免配音与画面脱节。
  任务级可用 `--rate "+10%"`（GUI「语速覆盖」）覆盖。
- **开场钩子质检**：`match` 阶段用非 LLM 规则检测首段是否为「今天给大家讲…」式报幕开场，
  命中则在日志与阶段数据中提示（不阻断），推动文案真正落在风格钩子上。
- **沉浸式第一人称**：`--pov first [--pov-character 角色名]`（GUI「叙述视角 / 代入角色」）
  可让解说以某角色第一人称视角讲述（对标 narrator-ai 的 target_character_name），
  默认跟随风格自带视角。

| 风格 | 一句话定位 | 推荐音色 |
|------|-----------|----------|
| 电影解说 | 沉稳旁白，主线钩子 + 结尾回味 | 云健 |
| 短剧漫剧 | 黄金三秒冲突、一集一反转 | 云希 |
| 悬疑解说 | 压抑克制、埋线拼图、细思恐极 | 云健 |
| 吐槽解说 | 玩梗吐槽、夸张比喻 | 云希 |
| 产品解说 | 痛点开场、卖点逐条 + 场景佐证 | 晓晓 |
| 人物解说 | 传记时间线、功过并陈 | 云健 |
| 地理解说 | 空间叙事、成因追溯、数据具象 | 云健 |
| 知识解说 | 反常识提问、类比拆解 | 云扬 |
| 搞笑式解说 | 离谱开场、魔改推进、高能梗 | 云夏 |
| 温柔式解说 | 轻声陪伴、细节留白 | 晓晓 |
| 盘点式解说 | 倒序排名、每条一个记忆点 | 云健 |
| 宣传式解说 | 亮点前置、排比号召 | 云扬 |
| 批判式解说 | 现象—证据—推论、克制反讽 | 云健 |
| 聊天式解说 | 朋友闲聊、设问互动 | 晓伊 |
| 激情式解说 | 蓄势爆破、高燃呐喊 | 云健 |
| 纪录片解说 | 克制辽阔、以小见大的观察式旁白 | 云健 |
| 游戏解说 | 高光操作、局势预判、术语点评 | 云希 |
| 体育赛事解说 | 战术解读、关键回合、赛点渲染 | 云扬 |
| 美食解说 | 勾馋口感、食材与烟火气 | 晓晓 |
| 历史解说 | 时间脉络、以古鉴今 | 云健 |
| 军事解说 | 态势推演、装备战术、冷静结论 | 云健 |
| 财经解说 | 现象—机制—证据链、理性提示 | 云扬 |
| 情感故事解说 | 克制叙事、细节戳心、留白共情 | 晓晓 |

新增/调整风格：编辑 `taggycat/prompts.py` 的 `STYLE_PRESETS` 与 `_STYLE_EXTRAS`
（语速 / 开场钩子模板库 / 结尾模板库）；`tests/test_prompts.py` 会校验字段齐全、
各维度彼此不同、语速与模板库齐备。

## 配置要点（config.toml）

| 段 | 说明 |
|----|------|
| `[llm]` / `[llm_local]` | 云端 OpenAI 兼容 / 本地 Ollama 备选（`[llm]` 无有效 Key 时自动回退，见 `taggycat doctor`）；裸地址自动补 `/v1`，不支持 `response_format` 时自动降级，`reasoning_effort = "none"` 关闭推理模型思维链（限制生成长度），截断时自动扩容 max_tokens |
| `[vlm]` | 画面理解；`provider = "openai_compatible" \| "comfyui"`，`enabled=false` 或未配置 → 降级为纯台词轨 |
| `[asr]` | `engine = "faster_whisper" \| "comfyui" \| "off"`（`enabled=false` 时仅走字幕轨）；工作流参数见 `[asr.comfyui]` |
| `[tts]` | `engine = "edge" \| "comfyui" \| "comfyui_clone"`（任务级可用 `--engine` 覆盖；GUI 默认引擎/音色/缓存见「设置 → 语音配置」） |
| `[tts.comfyui]` / `[tts.comfyui_clone]` | `base_url`、`workflow`（API 格式 JSON，相对路径按配置文件所在目录解析）、`text_input/ref_input`（可留空自动识别） |
| `[material]` | clip 时长范围、场景切分阈值、目标时长（plan 预算）、素材选取比例（素材过剩时候选采样覆盖比例） |
| `[output]` | 分辨率/编码/CRF、BGM 音量、原声处理三档 `original_audio_mode = "keep" \| "mute" \| "tail"`（默认 **mute=讲解段全静音**，tail=仅段尾保留 `original_audio_tail` 秒原声）、原声让路 `voice_yield` + `voice_yield_min`（台词重叠 ≥ max(阈值, 片段时长一半) 的片段整段保留原声、不配音不加字幕，该时段不讲解）、原片烧录字幕遮盖 `hard_sub_remove` + `hard_sub_band`（讲解段 delogo 遮盖带 x1,x2,y1,y2 占比，让路段保留原字幕）、讲解音高对齐 `voice_pitch_match` + `voice_pitch_max_semitones`（逐段估计原声与配音的基频差，各自 ffmpeg 移调配音向该段原声靠拢，时长不变，残差可低至 0~0.5 半音，幅度钳制默认 ±8 半音）、字幕字体 `subtitle_font` 与字号 `subtitle_fontsize`（默认 64，口播字幕自动去标点）、双语字幕 `subtitle_bilingual` + `subtitle_lang2`（烧录走 ASS：中文原字号、英文行 0.65 倍小字；另存 `narration.bilingual.srt` 外挂用，LLM 翻译带缓存，失败降级单语） |
| `[review]` | `validate_script`：校验文案开关（GUI 与任务级可覆盖） |
| `[paths]` | workspace / output / 音色样本 / `tts_cache` |
| `[ui]` | `theme`：界面主题 `light` \| `light-green` \| `light-purple` \| `dark` \| `dark-amber`（默认 `light`，菜单「设置 → 主题」实时切换并写回） |

标注【预留】的字段（`material.narration_ratio`、`output.language`、`queue.max_concurrent`、
`paths.fonts`、`paths.bgm_dir`）已入模型但暂未参与流程，`taggycat doctor` 会顺带检查
未知/拼写错误的配置项。

### ComfyUI 本地 TTS

1. ComfyUI 启动（默认 `http://127.0.0.1:8188`）
2. 打开你的 TTS 工作流 → 设置开启 **Dev mode** → **Save (API Format)** 导出 JSON
3. `config.toml` 填 `[tts.comfyui] workflow = "resources/workflows/xxx.json"`，`engine = "comfyui"`
4. 克隆音色：`engine = "comfyui_clone"` + `reference_audio`（或工作流内填参考音频）

### ComfyUI 画面理解（VLM 工作流）

1. 导出视觉理解工作流为 **API 格式** JSON（工作流内含 LoadImage → 视觉模型 → 文本输出）
2. `[vlm] provider = "comfyui"`，`[vlm.comfyui]` 填 `base_url` 与 `workflow`
   （或在「设置 → 接口设置 → 画面理解 VLM」切换为 ComfyUI 工作流并选择文件）
3. `image_input`/`text_input` 留空可自动识别（LoadImage 节点 / 按优先级找
   custom_prompt·prompt·text，跳过展示回显节点）；每帧提交一次，帧数受 `max_frames` 限制
   ⚠️ 仅支持**图片输入**工作流；视频输入工作流（如 VHS_LoadVideo）暂不适用逐帧理解
4. `taggycat doctor` 会检查工作流文件与 ComfyUI 可达性

### ComfyUI 台词转录（ASR 工作流）

1. 导出转录工作流为 **API 格式** JSON（LoadAudio → 转录节点 → 文本输出）
2. `[asr] engine = "comfyui"`，`[asr.comfyui]` 填 `base_url` 与 `workflow`
   （或在「设置 → 接口设置 → 台词转录 ASR」切换引擎并选择文件）
3. `audio_input` 留空自动识别 LoadAudio 节点；整条音轨一次提交（`timeout` 默认 1800s）；
   音频体积超过 `max_upload_mb`（默认 90，略低于 ComfyUI 100MB 请求体上限）时自动
   切分为多段依次转录再按时间偏移合并 —— 长视频/长音轨不会报 HTTP 413
4. 输出优先解析带时间戳的 JSON（`{"segments":[{start,end,text}]}`）；纯文本输出会按
   字数比例分配时长（有警告日志）
5. **CUDA 缺 `cublas64_12.dll` 时本地 faster-whisper 无法转录**（`taggycat doctor`
   会警告）：可 `uv pip install nvidia-cublas-cu12`，或按上面步骤改走工作流

### 字幕字体

烧录字幕需要含中文字形的字体：把 `NotoSansSC-Regular.otf` 放到 `resources/fonts/`，
或改 `[output] subtitle_font` 指向系统字体（Windows 可直接用
`C:/Windows/Fonts/msyh.ttc` 微软雅黑）。缺失时 ffmpeg 会回退系统字体（中文显示
可能异常），`taggycat doctor` 会给出警告。双语烧录的 ASS 样式按字体文件名自动映射
Fontname（`msyh*` → Microsoft YaHei、`NotoSansSC*` → Noto Sans SC）。

## CLI 一览

```
taggycat init                 生成 config.toml（已存在时返回 0，--force 覆盖）
taggycat doctor               环境与配置自检
taggycat probe <files...>     媒体信息探测
taggycat frames <file>        抽帧 / 场景切分
taggycat stages               列出流水线阶段
taggycat task create <files...>   创建任务（--voice/--engine/--no-tts-cache/
                                       --style/--target-duration/--no-validate-script…）
taggycat task list [--all]     任务列表（默认隐藏归档；--json 输出状态）
taggycat task show <id>        任务详情
taggycat task run <id>         执行流水线
taggycat task approve <id>     审核通过
taggycat task rename <id> <新名>     重命名（支持 ID 前缀/原名匹配）
taggycat task delete <id> [-y] 删除任务及其产物（运行中禁止）
taggycat task archive <id> [--undo]  归档 / 取消归档
taggycat ui                   启动桌面界面
```

`task run` 常用参数：

- `--until <stage>`：执行到该阶段（含）后停止
- `--stage <name> [--force]`：只跑指定阶段（`--stage` 本身即会重跑已完成阶段）
- `--all`：兼容保留——14 个阶段已全部实现，该开关无实际效果

退出码：

| 码 | 含义 |
|----|------|
| 0 | 成功（含 `--until` 提前停止、`init` 时配置已存在） |
| 1 | 一般性失败（doctor 检出 FAIL、缺 UI 依赖、配置文件不存在等） |
| 2 | 运行失败（阶段失败、内部异常；argparse 用法错误也是 2） |
| 3 | 暂停等待人工审核（非失败，`task approve` 后继续） |

## 开发

```powershell
uv run pytest tests -q         # 241 项测试（UI 测试走 QT_QPA_PLATFORM=offscreen）
uv run ruff check taggycat tests
```

测试约定：ASR/VLM 关闭或无 Key（触发降级）、LLM/TTS 一律 monkeypatch，成片链路用
ffmpeg 合成的测试视频真实跑通。

打包与发布：

```powershell
build_exe.bat                 # 生成 dist/taggycat.exe（单文件）
python scripts/render_icon.py # 从 taggycat.png 重建全套图标
```

CI（lint + 测试）与 Release（自动构建 exe、生成 SHA256、上传资产）位于 `.github/workflows/`。
贡献流程、版本发布步骤见 [CONTRIBUTING.md](CONTRIBUTING.md)。

## 项目结构

```
taggycat/
├── cli.py                 CLI 入口（init/doctor/task/ui...）
├── config.py              TOML → Pydantic 配置 + 键路径写回（保留注释）
├── doctor.py              环境自检
├── models.py              任务/阶段/片段模型（含 REVIEW/归档）
├── prompts.py             解说风格预设 + LLM 提示词
├── llm.py                 LLMClient / VLMClient（云端 + 降级）
├── asr.py                 faster-whisper / ComfyUI 工作流转录
├── comfy.py               ComfyUI 公共 HTTP 层（上传/提交/轮询）
├── timeline.py            台词/画面时间线
├── tts.py                 edge / ComfyUI / 克隆 三引擎 + 缓存
├── media/                 FFmpeg 封装（探测/抽帧/场景/字幕）
├── pipeline/              编排器 + 14 阶段实现（stages / stages_m3 / stages_m4）
├── store/                 任务状态与阶段产物（原子写 + 改名/归档/删除）
└── ui/                    PyQt6 主窗口 / 后台 Worker / 审核 / 接口设置 / 批量队列
```

仓库根目录另有：

```
taggycat.spec              PyInstaller 单文件打包配置（产物 dist/taggycat.exe）
build_exe.bat              一键构建单文件 exe
taggycat.ico / .png        品牌图标（resources/icons/ 为运行时派生图）
config.example.toml        脱敏配置模板（taggycat/ 内另有一份随包分发）
scripts/render_icon.py     从主图重建全套图标
packaging/                 Windows 版本资源（文件属性中的产品名与版权）
.github/                   CI / Release 工作流、Issue 与 PR 模板
LICENSE / NOTICE           Apache-2.0 许可与第三方声明
```

## 里程碑

- ✅ **M1** 骨架与媒体层（配置/模型/FFmpeg/CLI/任务存储）
- ✅ **M2** 理解（ASR + VLM + 时间线）
- ✅ **M3** 文案（分析/规划/生成/匹配/审核）
- ✅ **M4** TTS 与成片（配音/字幕/合成/导出）
- ✅ **M5** PyQt6 界面
- ✅ **M6** 打磨（doctor 自检 / 文档 / 测试与 lint）

## 开源与许可

- 本项目采用 **Apache License 2.0**，完整条款见 [LICENSE](LICENSE)；第三方依赖与素材声明见 [NOTICE](NOTICE)。
- 架构设计参考 [NarratoAI](https://github.com/linyqh/NarratoAI)（MIT），本项目代码为独立实现，未复制其源码。
- 参与贡献：[CONTRIBUTING.md](CONTRIBUTING.md) · 漏洞报告：[SECURITY.md](SECURITY.md) · 更新日志：[CHANGELOG.md](CHANGELOG.md)
- 依赖库遵循各自许可；FFmpeg、ComfyUI、Ollama 等外部程序由使用者自行安装，不随本项目分发。
- **请勿提交 `config.toml`**：其中含你的 API Key 与内网地址，已在 `.gitignore` 中排除。

## 发布前检查清单

首次推送到公开仓库前请逐项确认：

- [ ] 在仓库设置中补充维护者联系方式与安全邮箱（`SECURITY.md`、`pyproject.toml` 的 authors）
- [ ] 确认图标来源与授权，并在 `NOTICE` 中登记（第三方 / AI 生成素材尤其重要）
- [ ] `git status` 中不含 `config.toml`、`workspace/`、`output/`、真实 API Key 或内网地址
- [ ] 确认 `uv.lock` 已生成并提交，保证依赖可复现（本次已生成）
- [ ] `uv run ruff check taggycat tests` 与 `uv run pytest tests -q` 通过
- [ ] 本地执行 `build_exe.bat`，冒烟测试：启动界面、生成 `config.toml`、关于框版本正确
- [ ] `git tag v0.1.0 && git push origin v0.1.0` 触发 Release 工作流，核对 exe 与 SHA256
