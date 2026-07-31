
# Codex Monitor

一个面向 Windows 的 Codex 额度与会话监控桌面工具。

Codex Monitor 通过 `codex app-server` 读取当前 Codex 账号的用量信息，同时聚合 Codex 客户端、网页、VS Code、插件以及本地 session 的活动状态。它适合在使用 Codex 进行 Vibe Coding 时常驻桌面，帮助你快速判断额度、重置时间和当前会话是否需要人工介入。

## 功能

### 额度仪表盘

- 显示 Codex 额度窗口的已用百分比和剩余重置时间。
- 支持展示主要窗口、次要窗口和个人额度窗口。
- 显示账户 credits、是否有可用 credits 以及当前限制状态。
- 汇总日、周、月用量；当服务端没有直接返回统计时，使用本地采样历史估算，并以 `*` 标记。

### 会话与活动监控

- 聚合当前会话列表、Codex 客户端状态、VS Code插件会话和本地 session 文件。
- 通过 Codex app-server 通知和 VS Code IPC 获取更及时的活动变化。
- 将会话归类为思考中、输出中、执行中、运行中、等待审批、等待输入或空闲。
- 展示会话标题、最近活动时间、消息预览和项目目录，方便定位正在运行的任务。
- 活动状态默认每秒更新，额度数据默认每 60 秒刷新。

### 额度重置明细

- 展示可用的额度重置次数。
- 显示重置额度的获得时间、使用期限、状态和有效期时间线。
- 支持展开或折叠重置明细。

### 桌面工具体验

- 无边框深色小窗，适合放在桌面角落常驻。
- 支持系统托盘，可显示/隐藏窗口、刷新数据和退出程序。
- 支持窗口置顶、透明度调整、开机自启和窗口位置记忆。
- 设置页提供“打开官方用量页”入口。

## AI编程协助

本工程的编程工作使用codex协助完成。

## 界面设计参考

界面设计参考 [Agenton](https://agenton.xmagicer.com/)

## 环境要求

- Windows 10/11
- Python 3.10 或更高版本
- 已安装并可在 PowerShell 中执行的 Codex CLI
- 已完成 Codex 登录：

```powershell
codex login
```

## 安装与运行

依赖：
- PySide6
- requests
- json5
- watchdog

在项目目录中执行：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install PySide6
python .\codex_balance_monitor.py
```

程序启动后会自动连接 `codex app-server`。首次使用前请确认：

- PowerShell 中可以找到 `codex` 命令。
- Codex 已完成登录。
- 当前用户对 `CODEX_HOME`（通常为 `%USERPROFILE%\.codex`）具有读写权限。
- 不要从受限的 Codex 沙盒环境启动本工具，以免 app-server 无法写入状态库。

## 打包 Windows 程序

项目提供了 [codex_balance_monitor.spec](codex_balance_monitor.spec) 文件，可使用 PyInstaller 打包：

```powershell
python -m pip install pyinstaller
pyinstaller .\codex_balance_monitor.spec
```

生成的可执行文件名称为 `CodexBalanceMonitor.exe`。打包时会一并收集 `resources` 和 `config` 目录中的运行资源。

## 项目结构

```text
codexMonitor/
├─ codex_balance_monitor.py              # PySide6 主程序、app-server 客户端和界面
├─ codex_balance_monitor.spec            # PyInstaller 打包配置
├─ codex_monitor_version.txt             # Windows 版本信息
├─ config/
│  ├─ codex_balance_monitor_settings.json # 窗口与显示设置
│  └─ codex_balance_monitor_quota_history.json # 额度采样历史
└─ resources/
   ├─ codex_monitor_logo.png
   ├─ codex_monitor_logo.ico
   └─ codex_monitor_tray.ico
```

## 数据与隐私

- 程序通过本机 `codex app-server` 获取账号和额度信息，不自行实现登录流程。
- 窗口设置保存在项目目录下的 `config/codex_balance_monitor_settings.json`。
- 额度采样历史保存在 `config/codex_balance_monitor_quota_history.json`，用于补充日/周/月用量估算。
- 会话监控读取 Codex 的本地状态和 session 信息，仅用于当前窗口展示。

## 诊断日志

程序启动后会自动创建诊断日志：

- `logs/codex_monitor.log`：主线程、后台线程、Qt 消息和运行错误。
- `logs/codex_monitor_native.log`：由 Python `faulthandler` 写入的 native 崩溃现场。

如果程序安装目录不可写，日志会回退到 `%USERPROFILE%\.codex\codex-monitor\logs`。

## 常见问题

### 显示“未找到 codex 命令”

确认 Codex CLI 已安装，并在同一个 PowerShell 中执行 `Get-Command codex` 可以找到命令。

### 显示需要重新登录

在 PowerShell 中运行：

```powershell
codex login
```

登录完成后回到程序点击“刷新”。

### 额度读取失败但账号已连接

先点击“刷新”，再检查 Codex CLI 版本、网络连接和官方用量页。如果问题仍然存在，可根据窗口中的连接诊断信息检查 `CODEX_HOME` 权限和 app-server 错误。

## 许可证

本项目采用 MIT 许可证，详情请参见 [LICENSE](LICENSE) 文件。
