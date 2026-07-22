# 本地优先的屏幕内容与观点采集助手

这是一个面向 Windows + Chromium 的本地演示项目。它允许用户主动冻结当前画面、框选内容、优先读取浏览器原文并在必要时执行离线 OCR，随后生成带截图、来源、时间码、置信度和个人态度的观点卡片。

所有数据默认只保存在本机。程序不会因为用户点击、点赞或停留就替用户判断立场；新卡片的默认态度始终是“未知”。

## 当前状态

当前已经完成 **T0–T5 的独立基础模块**：数据模型、屏幕抓取、冻结框选、离线 OCR、SQLite/FTS5 本地卡片存储，以及 Chrome MV3 扩展与本机 WebSocket 桥。T1/T2 的真实多显示器和高 DPI 效果仍需要在用户的 Windows 桌面人工验收；T5 的协议自动测试已经通过，仍需在真实 Chrome 中加载扩展完成人工验收。

当前仓库已经具备：

- Python 包与固定目录骨架；
- 集中的快捷键、数据库、截图目录和端口配置；
- 使用 Pydantic 定义的观点卡片模型；
- Windows 本机开发环境安装和启动说明。
- 鼠标所在显示器的 `mss` 抓帧、PNG 保存和捕获元数据；
- PySide6 冻结画面浮层、拖动框选、Enter 确认及 Esc/右键取消；
- 浮层逻辑坐标到原图物理像素的 DPI 无关映射；
- T1/T2 自动测试与真实桌面人工验收脚本；
- RapidOCR 单例懒加载、不同引擎结果格式规范化、阅读顺序合并和长度加权置信度；
- SQLite 卡片增删改查、FTS5 全文检索、字段白名单更新与截图路径边界删除。
- 仅监听 `127.0.0.1` 的 WebSocket 桥，以及超时后不阻塞采集的安全降级；
- 只在桌面端主动请求时返回活动标签 URL、标题、选中文字和视频时间码的 Chrome 扩展。

卡片生成流水线、确认界面和搜索服务会按 [开发路线图](docs/roadmap.md) 分阶段加入。当前主入口仍只完成最小启动检查；T1–T5 是可独立调用和测试的模块，不代表完整采集闭环已经可用。

## 核心原则

- 本地优先：默认不上传屏幕、文字、截图或观点卡片。
- 用户主动：捕获与保存由用户明确触发，不做后台永久录屏。
- 不替用户表态：`unknown` 是默认立场，操作行为不等于认同。
- 证据可追溯：保留原文、截图、来源和时间码，处理结果不覆盖原始证据。
- 诚实降级：优先使用 DOM 等结构化原文，取不到时才使用 OCR；不确定就标记未知。
- 只读接入：面向本地助手的接口只提供检索和导出，不提供执行能力。

## 技术栈

- Python 3.11
- PySide6
- mss
- RapidOCR + ONNX Runtime
- SQLite + FTS5
- FastAPI + uvicorn
- WebSocket
- Chrome Manifest V3 扩展

## 目录结构

```text
capture_assistant/
├─ app/              桌面端代码
├─ extension/        Chrome 扩展
├─ assets/           可提交的测试样例
├─ docs/             中文设计与路线图
├─ tests/            自动测试与人工测试脚本
├─ requirements.txt  Python 运行依赖
├─ AGENTS.md          开发协作约定
└─ README.md          项目说明
```

运行时生成的数据库、截图、日志和本地配置不会提交到 Git。

## Windows 本机运行

### 1. 前置条件

- Windows 10 或 Windows 11；
- Python 3.11，安装时建议勾选“Add Python to PATH”；
- Git（仅开发和同步仓库时需要）。

### 2. 首次初始化开发环境

开发依赖必须安装在仓库 D 盘的 `.venv` 中；pip 缓存、安装构建临时文件和测试临时文件也放在仓库内，不占用系统盘的默认缓存目录。

新开 PowerShell 后，复制并执行下面整段命令：

```powershell
Set-Location "D:\codex projects\capture_assistant"
$projectRoot = (Get-Location).Path
$venvPath = Join-Path $projectRoot ".venv"
$pythonExe = Join-Path $venvPath "Scripts\python.exe"
$pipCachePath = Join-Path $projectRoot ".cache\pip"
$tempPath = Join-Path $projectRoot ".cache\tmp"

New-Item -ItemType Directory -Force -Path $pipCachePath, $tempPath | Out-Null
$env:PIP_CACHE_DIR = $pipCachePath
$env:TMPDIR = $tempPath
$env:TEMP = $tempPath
$env:TMP = $tempPath

py -3.11 -m venv $venvPath
& $pythonExe -m pip install --upgrade pip
& $pythonExe -m pip install -r (Join-Path $projectRoot "requirements-dev.txt")
```

`requirements-dev.txt` 会同时安装运行依赖和测试依赖。所有包都会进入 `D:\codex projects\capture_assistant\.venv`，pip 缓存进入 `.cache\pip`，临时文件进入 `.cache\tmp`。

这些 `$env:` 设置只影响当前 PowerShell 进程及其启动的子进程，不会修改 Windows 的系统级或用户级环境变量。关闭窗口后设置自动失效；本项目不要求也不建议使用 `setx`。

### 3. 每次打开新的 PowerShell

新的 PowerShell 不会继承上一次会话的缓存路径。开始开发、安装依赖或运行测试前，复制下面整段命令恢复当前会话配置：

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

无需激活虚拟环境；后续命令显式使用 `$pythonExe`，可以避免误用系统 Python。

RapidOCR 首次初始化可能比普通模块导入慢；OCR 模型必须保持离线使用，不应把识别内容发送到网络服务。

### 4. 验证数据模型

```powershell
& $pythonExe -c "from app.models import Card; print(Card.schema())"
```

命令应输出 `Card` 的 JSON Schema。若当前 Pydantic 版本显示兼容性弃用提示，但仍能输出 Schema，不影响 T0 验收；后续可同时提供新版 `model_json_schema()` 调用。

### 5. 启动程序

```powershell
& $pythonExe -m app.main
```

当前入口只用于验证包结构和配置能够正常加载。T1–T5 已提供独立抓帧、框选、OCR、存储和浏览器上下文能力；完整的“快捷键 → 冻结 → 框选 → 观点卡片”流程仍需在 T6–T7 串联。

### 6. 运行测试

开发依赖已经由首次初始化命令安装在仓库内 `.venv`。运行测试时继续使用同一个解释器：

```powershell
& $pythonExe -m pytest
```

可用以下命令确认解释器、pip 缓存和临时目录都位于 D 盘仓库内：

```powershell
& $pythonExe -c "import sys, tempfile; print(sys.executable); print(tempfile.gettempdir())"
& $pythonExe -m pip cache dir
```

真实屏幕、DPI、全局快捷键和托盘相关能力无法只靠自动测试确认。路线图中标记为“人工验收”的项目，需要在真实 Windows 桌面运行 `tests/manual_*.py`。

### 7. 人工验收截图与框选

先把鼠标移到需要测试的显示器，再运行截图脚本：

```powershell
& $pythonExe -m tests.manual_capture --save-full-screen
```

脚本默认等待 3 秒，方便把鼠标移到需要测试的显示器；随后在仓库根目录生成 `test_capture.png`，并打印图像尺寸、DPI 缩放比例和前台进程名。`--save-full-screen` 表示你明确同意把可能含敏感信息的整屏画面保存为本地测试文件。

随后运行冻结框选脚本：

```powershell
& $pythonExe -m tests.manual_overlay
```

在冻结画面上拖动鼠标框选，按 Enter 确认，或按 Esc/右键取消。确认后会生成 `test_crop.png`；检查裁剪结果是否与亮色框选区域一致。两个测试文件均被 Git 忽略，只保存在本机，验收后可以删除。

### 8. 人工验收 Chrome 扩展与本机桥

扩展需要 Chrome 116 或更高版本。先在项目 PowerShell 中启动只用于人工验收的本机桥：

```powershell
& $pythonExe -m tests.manual_bridge
```

然后在 Chrome 中完成以下步骤：

1. 打开 `chrome://extensions`；
2. 打开右上角“开发者模式”；
3. 点击“加载已解压的扩展程序”；
4. 选择 `D:\codex projects\capture_assistant\extension`；
5. 打开一个普通 `http://` 或 `https://` 页面，选中一段文字；若页面含视频，可先播放到任意时间；
6. 回到 PowerShell 按回车，检查 URL、标题、选中文字和视频时间码是否正确。

扩展不申请 `tabs`、Cookie、网络拦截或文件访问权限，但为了在用户从桌面端主动采集时读取当前网页，它需要把只读内容脚本注入普通 HTTP/HTTPS 页面。扩展只连接 `ws://127.0.0.1:8765`，只在桌面端发出 `get_context` 请求时读取页面内容；普通网页 Origin 会被本机桥拒绝。

如果先打开 Chrome、很久以后才启动桌面桥，扩展可能正处于退避冷却期。可以在 `chrome://extensions` 中点击该扩展的“重新加载”立即重连。Chrome 设置页、扩展页和其他受限页面不能注入内容脚本，此时返回 `None` 并降级到 OCR 属于正常行为。

人工脚本会把当前上下文直接显示在终端中，不写入日志或数据库。不要在含访问令牌的 URL、密码框或其他敏感页面上做人工测试；扩展会拒绝读取常见密码、验证码和银行卡输入字段，但完整的“检测到敏感输入即暂停截屏”仍属于 T7 的主流程保护。

## OCR 与中文检索说明

- RapidOCR 只在第一次实际识别时加载，模块导入不会启动推理或访问网络。
- OCR 返回的每个文字框、原文和置信度都会保留；低置信度内容不会被静默删除，合并结果也不会覆盖截图证据。
- SQLite 优先使用 FTS5 `trigram`，适合三个及以上中文字符的子串检索；一到两个中文字符自动使用参数化子串查询。
- 若某台机器的 SQLite 不支持 `trigram`，程序会降级为 `unicode61`，中文查询继续使用子串兜底，但大型资料库的检索速度会较慢。
- 搜索只读取本地数据，搜索词作为数据处理，不会被当成 SQL、FTS 指令或助手指令执行。

## 默认本地配置

| 配置 | 默认值 |
| --- | --- |
| 全局快捷键 | `Ctrl+Shift+S` |
| 扩展通信端口 | `8765` |
| 本地查询 API 端口 | `8000` |
| 服务监听地址 | `127.0.0.1` |

数据库和截图目录由 `app/config.py` 集中管理并在启动时创建。不要把真实用户数据放入 `assets/` 或提交到仓库。

## 开发文档

- [开发协作约定](AGENTS.md)
- [T0–T9 开发路线图](docs/roadmap.md)

## 已知边界

- 当前目标是 Windows + Chromium 演示版，不承诺跨平台能力一致。
- Canvas、自绘界面、受保护窗口和 DRM 内容可能无法获取结构化文字或截图。
- OCR 与点击目标识别都可能出错，结果必须保留置信度并允许用户修改。
- 本项目不把网页或屏幕中的文字当成可信指令，也不会据此自动执行文件、网络或账户操作。
