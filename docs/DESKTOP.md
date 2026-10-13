# Windows 桌面版

本项目自身全部代码由 AI 开发，采用 MIT 许可证。第三方 QQ/NapCat 等组件
不是本项目开发的代码，遵循各自许可；内置组件版整体不适用 MIT 商用授权。

## 开始使用

双击 `LuxunQQ.exe`，无需安装 Python。先点「离线演示」即可体验：模拟消息、
三条候选、编辑、发送、重新生成、丢弃，以及自动托管。
演示只使用内置回复和独立的演示记忆库，不连接 QQ 或任何模型接口。

当前内置组件版面向 Windows x64。真实使用时，在「连接与模型」完成以下设置：

1. **模型生成**：选择大模型 API、本地模型服务，或本地 GGUF 文件。
2. **运行组件**：点击「启动内置 QQ/NapCat」。首次会释放内置文件，自动配置本机连接
   和随机 Token；不需要预先安装 QQ/NapCat。等待完成后点击「打开 QQ 登录页」，
   在系统浏览器中的本机 NapCat WebUI 扫码登录。若页面尚未就绪，稍后刷新。
3. **QQ 与回复规则**：填写白名单（或明确选择回复所有私聊），选择角色、发送延迟、
   记忆和两阶段生成。内置版的 WebSocket 地址和 Token 自动填入，无需手动开启 OneBot。
4. 保存配置，点击「启动回复服务」。初次建议先人工确认候选，再开启「自动托管」。

也可指定外部 QQ/NapCat 启动程序和连接信息。登录页仍使用浏览器；候选回复、编辑、
发送和托管操作均在应用窗口中完成。

账号从 `get_login_info` 自动读取，无需在软件里填写登录密码。
只处理私聊，不处理群聊；本人消息不触发回复或控制操作。
桌面版独立使用配置，不读取命令行版的 `config.yaml` 或其环境变量覆盖。

## 窗口里的操作

- 左侧按 QQ 号列出待回复消息；右侧显示三条可编辑候选，点击对应「发送这条」。
- 「重新生成」仅生成新候选，即使开着自动托管也不会立即发送重生成结果。
- 「自动托管」对后续符合回复范围的消息固定发送第一条候选；关闭后恢复人工选择。
- 「暂停」不处理暂停期间新收到的消息，也会取消尚在延迟等待中的发送；恢复后不补发这些消息。
- 「停止」断开回复服务与 QQ 的连接，并停止由应用启动的本地推理进程。
  内置或手动启动的 QQ/NapCat、外部模型服务不会被强制结束；关闭应用也不会注销 QQ。
- 模型调用在后台执行。停止/关闭时禁止后续发送，并等待在途模型调用退出后清理。
  已交给 QQ 的发送请求无法撤回；超时时需先检查 QQ 聊天记录再决定是否重试。
- 同一人的新消息会替换其旧候选。应用校验候选版本，防止误发已过期的回复。
- 候选、分析、操作提示和错误仅在应用里展示，**不再给 QQ 自己发控制消息**。

## API 与本地模型

**API**：支持 OpenAI 兼容接口，填写地址、模型名称、API Key。
例如 `https://api.deepseek.com` + `deepseek-chat`。
实际费用由服务商收取，应用没有内置额度或密钥。

**本地服务**：可连接现有 Ollama（协议 `ollama`）或 LM Studio/llama.cpp
（协议 `openai`）。填写服务地址和实际模型名，软件不负责启动外部服务。

**本地文件**：选择 `llama-server.exe` 和 GGUF 模型。
保留推理引擎的 DLL、模型的全部分片。应用自动启动仅监听本机的推理进程，
轮询加载状态，再连接 QQ。默认 GPU 层数为 `0`（CPU），`-1` 表示全部尝试放到 GPU；
具体支持取决于所选 llama.cpp 构建与机器硬件。加载日志在 `live/local-model.log`。

聊天记忆使用 SQLite：保留近期对话和相关历史召回，并可使用已有的事实记忆。
桌面版暂不启用原命令行版的后台事实抽取线程，也不自动导入原 QQ 聊天数据库。
模型效果及可用性取决于实际接入，离线演示不代表真实模型的回复质量。

## 单 EXE 与运行组件的边界

本次内置组件版 `LuxunQQ.exe` 包含界面、Python、**QQ 9.9.30-48517 与 NapCat 4.18.28**。
**不包含 llama-server 推理引擎或模型权重**。API 模式只需填写自己的接口配置；
本地模式需自行提供已有模型服务，或引擎与 GGUF 文件。

QQ/NapCat 文件通过固定 SHA256 校验后封装。QQ 安装包的腾讯数字签名已核验；
制作过程仅用 7-Zip 解压，没有执行安装器。完整组件以压缩包内置，点击启动才释放到
`components/qq-napcat-<版本摘要>/`；相同版本重复启动复用该目录及已有配置。
也可以点击「仅释放内置组件（不启动）」只提取文件。

这是单个交付 EXE，不表示所有组件都在内存中运行：每次打开 EXE，打包器会先解包
资源到临时目录，首次准备 QQ/NapCat 还要写入用户数据目录。建议系统盘预留至少 4 GB
空闲空间，首次打开和释放可能需要几分钟。它无法跳过 QQ 登录。

开发机没有安装、启动或登录 QQ/NapCat。已完成静态结构检查及模拟测试；
QQ/NapCat 的版本兼容、实际扫码登录和收发消息仍需在目标电脑验收，不能视作已经实机验证。
若启动失败，可查看组件目录下 `napcat-launch.log` 及 NapCat 日志；默认端口是 3001/6099，
目标电脑如有其他程序占用它们，应先解除冲突。

基础版可通过不带参数的打包命令生成，不包含任何 QQ/模型组件。已有外部组件也可以导入：

```text
components/
  llama/llama-server.exe
  llama/其他 DLL 与许可证
  models/model.gguf
  models/模型许可证
  qq/launcher.exe 或 launcher.bat
  qq/完整的 QQ/NapCat 环境、依赖和许可证
```

使用「导入组件目录」导入外部文件夹，会自动填入引擎、模型、
`qq/launcher.exe` 或 `qq/launcher.bat` 路径。每次导入使用独立目录，不覆盖原有组件及账号数据。
导入多个模型时请检查自动选中的文件；分片 GGUF 需选择首个分片。

### 第三方许可

NapCat 采用 Limited Redistribution License，**禁止商业用途**；发行包保留完整许可和
作者、来源说明，见 `THIRD_PARTY_NOTICES.txt` 及源码 `docs/third_party/`。
QQ 为腾讯专有软件，遵循其自身条款；本项目的 MIT 许可不授予 QQ 的再分发权。
本次集成产物保存在本地，未上传为公开二进制发行版。上游组件代码未作修改。

## 数据与密钥

默认目录：`%LOCALAPPDATA%\LuxunQQ`。
测试或便携使用可通过 `LUXUN_DESKTOP_HOME` 指定其他目录。

- `desktop.json`：桌面配置；API Key 和连接 Token 使用 Windows DPAPI 加密，绑定当前 Windows 用户。
- `live/`：真实会话记忆、发送记录及本地模型日志。
- `demo/`：独立的离线演示数据。
- `components/`：导入的可选组件。

内置 NapCat 的连接和 WebUI 仅监听 `127.0.0.1`，使用不同的随机 Token。
NapCat 配置及 `desktop-runtime.json` 为运行需要保存明文 Token，QQ 自身可能另外保存
账号数据；不要公开或分享这些运行目录。DPAPI 加密仅适用于桌面应用的 `desktop.json`。

数据不写到 EXE 解包目录，也不会自动上传到 GitHub。
API 模式会把生成所需的消息和相关记忆发送给所选模型服务商。
加密配置不能直接复制给另一位 Windows 用户使用密钥。

## 开发、打包与验证

Windows / Python 3.10+（需带 Tk）：

```powershell
python -m venv .venv-desktop
.venv-desktop\Scripts\python.exe -m pip install -e ".[desktop,build,dev]"
$env:PYTHONPATH = "src"
.venv-desktop\Scripts\python.exe -m luxun_qq.desktop

# 生成基础 EXE（不下载或启动任何 QQ/模型组件）
.venv-desktop\Scripts\python.exe tools/build_desktop.py

# 生成 QQ/NapCat 内置版（准备材料需要网络与 7-Zip；不安装/启动 QQ）
.venv-desktop\Scripts\python.exe tools/prepare_qq_bundle.py
.venv-desktop\Scripts\python.exe tools/build_desktop.py --qq-bundle build/qq-bundle

# 仅校验 EXE 内置资源，不释放或执行 QQ/NapCat
Start-Process -FilePath .\dist\LuxunQQ.exe -ArgumentList '--inspect-bundle "C:\temp\luxun-bundle.json"' -WindowStyle Hidden -Wait

# 模拟连接与回归测试，不需要 NapCat 或 QQ
.venv-desktop\Scripts\python.exe -m pytest tests -q

# 已打包 EXE 的离线界面往返检查，REPORT 使用绝对路径
Start-Process -FilePath .\dist\LuxunQQ.exe -ArgumentList '--smoke-test "C:\temp\luxun-report.json"' -WindowStyle Hidden -Wait
```

产物在 `dist/LuxunQQ.exe`，附带使用说明和第三方许可声明。
测试覆盖离线生成、窗口加载、人工/自动发送路由、候选过期、暂停/停止、
配置加密、模拟 OneBot 接口响应。真实 NapCat/QQ、真实 API 和 GGUF/GPU
须在具备环境的目标电脑验收；开发机不部署这些环境。
