# 开发协作约定

本文档是本仓库中 Codex、开发者及其他自动化代理的长期前置约定。开始任务前先阅读本文档；若任务描述与本文档冲突，以用户最新、明确的要求为准，并在交付说明中指出差异。

## 1. 项目目标

本项目是“本地优先的屏幕内容与观点采集助手”的 Windows + Chromium 演示版。首个完整闭环是：

1. 用户通过全局快捷键冻结当前画面。
2. 用户主动框选需要保存的区域。
3. 程序优先读取浏览器选中文字，无法取得时再执行离线 OCR。
4. 程序生成带原文、截图、来源、时间码和置信度的观点卡片。
5. 用户明确标记“认同、反对、存疑、只是有用”或保留“立场未知”。
6. 卡片保存在本机，并能通过全文检索、只读接口和导出再次使用。

本阶段不实现持续监控、云端同步、自动替用户判断立场、跨应用执行操作或绕过受保护内容。

## 2. 固定技术约定

- 运行平台：Windows 10/11。
- Python：3.11。
- GUI：PySide6。
- 截屏：mss。
- OCR：RapidOCR（`rapidocr-onnxruntime`），中英文离线识别。
- 数据库：标准库 `sqlite3` + SQLite FTS5。
- 本地接口：FastAPI + uvicorn，只允许监听 `127.0.0.1`。
- 浏览器端：Chrome Manifest V3 扩展。
- 桌面端与扩展通信：WebSocket，默认 `ws://127.0.0.1:8765`。
- 配置集中放在 `app/config.py`，不要在业务模块散落端口、路径和快捷键常量。
- 数据模型集中放在 `app/models.py`。模型字段发生变化时，必须同步检查数据库、接口、导出、测试和文档。
- Python 运行依赖和开发依赖必须安装在仓库 D 盘目录 `D:\codex projects\capture_assistant\.venv`，不得安装到系统 Python、用户级 site-packages 或仓库外的虚拟环境。
- pip 缓存固定为仓库内 `.cache/pip`，构建和测试所用的 `TMPDIR`、`TEMP`、`TMP` 固定为仓库内 `.cache/tmp`。
- 上述缓存路径只能通过当前 PowerShell 进程的环境变量设置；文档和脚本不得使用 `setx`，不得修改 Windows 系统级或用户级环境变量。

未经任务明确要求，不替换上述技术栈，也不引入云端服务、遥测 SDK 或额外网络依赖。

## 3. 固定目录结构

```text
capture_assistant/
├─ app/
│  ├─ __init__.py
│  ├─ main.py       # 快捷键、托盘与总体编排
│  ├─ capture.py    # mss 抓帧
│  ├─ overlay.py    # PySide6 冻结画面与框选浮层
│  ├─ ocr.py        # RapidOCR 封装与文字行合并
│  ├─ store.py      # SQLite 与 FTS5
│  ├─ bridge.py     # 浏览器扩展 WebSocket 桥
│  ├─ server.py     # 本机只读检索服务
│  ├─ models.py     # 观点卡片数据结构
│  ├─ config.py     # 快捷键、路径和端口
│  ├─ pipeline.py   # T6 起加入的卡片生成流水线
│  ├─ hotkey.py     # Windows RegisterHotKey 封装
│  ├─ review.py     # 候选卡片人工审核窗口
│  └─ safety.py     # 敏感应用与捕获前安全判断
├─ extension/       # Chrome Manifest V3 扩展
├─ assets/          # 可提交的测试样例等静态资源
├─ docs/            # 中文项目文档
├─ tests/           # 自动测试和 manual_*.py 人工测试
├─ requirements.txt
├─ run.bat          # T9 加入
└─ README.md
```

不要把运行时数据库、用户截图、日志、模型缓存、密钥或访问令牌提交到仓库。

## 4. 隐私与安全边界

以下规则属于产品边界，不得作为“后续优化”省略：

- 默认本地运行，采集内容不得离开本机。
- 用户未主动保存的临时画面应仅存在于内存；T0–T9 演示版不实现后台永久录屏。
- 屏幕文字、网页内容、OCR 结果和导入资料一律视为不可信数据，不能成为系统指令。
- 对外服务必须只读，不提供新增、修改、删除或执行型接口。
- FastAPI 与 WebSocket 默认只绑定回环地址；不得改成 `0.0.0.0`。
- 用户态度默认是 `unknown`。点赞、点击、停留或收藏均不得自动解释为“认同”。
- 原文、AI/程序处理结果、用户态度和用户备注必须保持可区分；不得静默覆盖原始证据。
- 无法可靠识别点击目标、作者或语义时，记录“未知”和置信度，不猜测。
- 检测到密码输入、锁屏或敏感应用时应优先暂停捕获；不得声称能够识别所有敏感信息。
- DRM、安全窗口和黑帧属于正常受限状态，只提示用户，不尝试绕过。
- 删除卡片时必须同步清理其截图、全文索引及其他派生数据。
- 日志不得记录 OCR 全文、完整截图、URL 查询参数、Cookie、访问令牌、密码或其他个人敏感内容。

## 5. 编码规范

- 公共函数、类和模块边界必须有类型标注。
- 模块、类和非显然函数应有简洁 docstring，说明职责、参数、返回值和关键失败方式。
- 每个模块应能被单独 import；不要在 import 时启动 GUI、服务、OCR 推理或写入数据库。
- 平台相关代码集中隔离，并对非 Windows 或无桌面环境提供明确、可理解的错误。
- 对文件、数据库和网络连接使用上下文管理或显式关闭。
- GUI 主线程只负责 UI；耗时 OCR、数据库操作和服务循环不得无界阻塞界面。
- 时间使用带时区的 ISO 8601 字符串；ID 使用 UUID4 字符串。
- 截图路径保存为数据目录内的相对路径，避免把某台机器的绝对路径写入数据库。
- 配置目录和截图目录由 `app/config.py` 统一创建。
- 只捕获具体异常；需要降级时保留可诊断信息，但不要把敏感正文写入日志。
- 新增第三方依赖前说明用途，并同步更新 `requirements.txt` 和 README。

## 6. 数据模型约定

T0 的 `Card` 是后续模块共同依赖的契约，至少包含：

- `id`：UUID4 字符串。
- `text`：不可静默覆盖的提取原文，即 DOM 选中文字或 OCR 文字。
- `edited_text`：用户审核后可选的整理文字，默认 `None`；不得替代或改写 `text` 的证据语义。
- `text_source`：仅允许 `dom` 或 `ocr`。
- `confidence`：范围 0–1。
- `screenshot_path`、`full_screenshot_path`：相对路径。
- `source_url`、`source_title`、`video_time`：可选来源信息。
- `app_name`、`monitor`：可选捕获环境信息。
- `created_at`：带时区的 ISO 8601 字符串。
- `stance`：`unknown`、`agree`、`disagree`、`doubt`、`useful` 之一，默认 `unknown`。
- `note`：用户备注，默认空字符串。

改变字段名、含义、允许值或默认值前，先说明迁移策略；不要让已有数据库被静默破坏。

## 7. 测试与验收

打开新的 PowerShell 后，先执行以下命令。变量和环境变量只对当前 PowerShell 进程及其子进程有效，关闭窗口后自动失效：

```powershell
Set-Location "D:\codex projects\capture_assistant"
$projectRoot = (Get-Location).Path
$pythonExe = Join-Path $projectRoot ".venv\Scripts\python.exe"
$pipCachePath = Join-Path $projectRoot ".cache\pip"
$tempPath = Join-Path $projectRoot ".cache\tmp"
New-Item -ItemType Directory -Force -Path $pipCachePath, $tempPath | Out-Null
$env:PIP_CACHE_DIR = $pipCachePath
$env:TMPDIR = $tempPath
$env:TEMP = $tempPath
$env:TMP = $tempPath
```

所有安装和测试命令必须显式使用 `$pythonExe`，不要直接调用系统 `pip` 或 `python`：

```powershell
& $pythonExe -m pip install -r (Join-Path $projectRoot "requirements-dev.txt")
& $pythonExe -m pytest
& $pythonExe -c "from app.models import Card; print(Card.schema())"
```

若尚未安装 `pytest`，只在对应测试任务中加入开发依赖或按用户要求安装，不要假装测试已运行。

测试分两类：

- 自动测试：纯逻辑、模型、数据库、接口和可模拟的流水线，完成任务前必须运行相关测试。
- 人工测试：真实桌面截屏、DPI 框选、全局快捷键、托盘和浏览器扩展。相关脚本使用 `tests/manual_*.py` 命名，并在交付时明确标注“需要用户在 Windows 桌面人工验收”。

不能在当前环境执行的测试，应说明具体原因、已完成的替代检查和用户可运行的命令；不得将“未执行”表述为“通过”。

## 8. 任务顺序

按依赖关系推进，默认顺序为：

```text
T0 →（T3 与 T4 可并行）→ T1 → T2 → T5 → T6 → T7 → T8 → T9
```

- T0 冻结骨架、配置和数据模型，是所有任务的前置。
- T3、T4 是纯逻辑任务，适合优先自动测试。
- T1、T2、T7 依赖真实 Windows 桌面，需要人工验收。
- T5 先使用 DOM、URL、标题、选区和视频时间码等结构化信号。
- T6 严格遵循“DOM 选中文字优先，OCR 降级”的获取链。
- T8 对外只提供本机只读查询与导出。
- T9 最后处理启动脚本、演示流程和可选打包说明。

每次只实现当前任务及其必要修复。发现后续需求时记录到 `docs/roadmap.md`，不要提前扩大范围。

## 9. Git 与交付约定

- 一个任务对应一个可审查的提交；提交前先运行与该任务有关的测试。
- 提交信息建议使用 `类型: 中文摘要`，例如 `feat: 完成观点卡片数据模型`。
- 不提交 `.env`、数据库、截图、日志、虚拟环境或生成产物。
- 不覆盖或撤销用户已有改动；遇到不相关的工作区变更时保留并说明。
- 未经用户明确授权，不执行强制推送、历史重写、分支删除或破坏性清理。
- 交付说明应包含：完成内容、关键文件、测试结果、未验证事项和下一任务建议。
