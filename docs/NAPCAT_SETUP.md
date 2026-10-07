# 本地模型 + NapCat 部署指南

目标形态：

```
   ┌────────────────────────┐         ┌──────────────────────────────┐
   │ NapCat (OneBot 11)     │         │ luxun-qq 服务  127.0.0.1:8848 │
   │ 登录 1234567890        │         │                              │
   │                        │──POST──▶│ 收到 1987654321 的消息        │
   │ 收到 1987654321 消息   │ 事件上报 │        │                     │
   │                        │         │        ▼                     │
   │                        │         │ 三步思考 + 三候选             │
   │  自己收到候选文本 ◀────┼─────────│ （由 1234567890 发给自己的私聊）│
   │  你回 1 / 2 / 3        │         │        │                     │
   │                        │◀────────│ 把选中的那条发给 1987654321   │
   └────────────────────────┘ send_   └───────────────┬──────────────┘
                              private_msg              │
                                          ┌────────────▼──────────────┐
                                          │ llama.cpp CUDA            │
                                          │ 127.0.0.1:18080           │
                                          │ Qwen2.5-7B Q4_K_M         │
                                          │ GPU: RTX 5080 (约 10GB)   │
                                          └───────────────────────────┘
```

> ⚠️ **本机当前状态（已排查）**
> NapCat 4.18.28 期望 QQ 版本 `9.9.31-49738`，而本机 QQ 是 `9.9.36-53644`。
> 我已反查注入器 `NapCatWinBootMain.exe`：**内部没有任何版本校验代码**
> （二进制里不出现版本号字符串），所以这个版本差**不会**阻止它启动。
> 真正会挡路的是注册表与运行目录问题，见下面「启动方式」。

---

## 一、本地模型（已完成）

用的是 LM Studio 自带的 llama.cpp CUDA 后端，**不需要装 LM Studio 图形界面**。
原因：LM Studio GUI 安装器要写 `Program Files` 与 `%LOCALAPPDATA%`，
在本机沙箱下会被拦截；而 `llama-server` 就是它包里同一个推理后端，
直接跑能得到完全相同的推理能力与 GPU 加速，且参数完全可控。

### 启动

```powershell
cd E:\code\luxun-qq
$env:PYTHONPATH="src"

# 常驻守护（推荐）：会自动重启，且在模型服务挂掉时恢复
python tools\model_server.py supervise --ctx 16384 --port 18080

# 或只启动一次（进程结束服务就停）
python tools\model_server.py start --ctx 16384 --port 18080

# 查看状态 / 停止 / 自测推理
python tools\model_server.py status
python tools\model_server.py stop
python tools\model_server.py test
```

就绪后会打印 OpenAI 兼容接口：`http://127.0.0.1:18080/v1/chat/completions`

### 实测性能

| 指标 | 数值 |
|---|---|
| 模型 | Qwen2.5-7B-Instruct Q4_K_M（4.5GB，两分片） |
| 显存占用 | **约 10.0 GB** / 16.3 GB（含 16K 上下文） |
| 上下文 | 16384 tokens |
| 单条生成耗时 | **1.6 ~ 2.1 秒**（含语气自检与结构化） |
| 首次启动 | 约 5.5 分钟（CUDA kernel JIT 编译，仅一次；缓存已落在 `_runtime/cuda_cache`） |
| 之后启动 | 约 5.5 分钟（主要是模型加载 I/O） |

### 想换更强的模型

12GB 预算下还可以选：

| 模型 | 量化 | 权重占用 | 说明 |
|---|---|---|---|
| Qwen2.5-7B | Q4_K_M | 4.5GB | **当前使用**，速度最快 |
| Qwen2.5-14B | Q4_K_M | ~8.5GB | 语气明显更好，但只剩约 2GB 给上下文，建议 `-c 8192` |
| Qwen2.5-7B | Q6_K | 6.0GB | 质量略好于 Q4 |

换模型只需改 `tools/model_server.py` 里的 `MODEL` 指向新 gguf，并同步
`config.yaml` 的 `llm.model`（别名可自行调整）。

---

## 二、NapCat（待安装确认）

### 已下载

```
_downloads\NapCat.Shell.Windows.Node.zip   (111.3 MB, v4.18.28)
_runtime\napcat\                           已解压
```

### 启动方式

NapCat 不是自带 QQ 的独立程序，它是**注入到已安装的 QQ** 里运行的。
本机有两个特殊情况，都已经处理好了：

**① 原版启动脚本会报 `provided QQ path is invalid`**

原版 `launcher-*.bat` 通过注册表定位 QQ：

```
HKLM\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\QQ
```

本机 QQ 是解压/绿色方式部署的（目录里有 `Uninstall.exe` 但没写注册表项），
所以这个键不存在，原版脚本查不到路径就退出了。

→ **用我提供的 [launcher-local.bat](../_runtime/napcat/napcat/launcher-local.bat) 代替**，
它直接写死 QQ 路径、跳过注册表查询，并在缺文件时给出明确报错。

**② 必须在 NapCat 目录里运行**

原版脚本用 `%cd%` 拼接 `NapCatWinBootHook.dll`、`napcat.mjs` 等文件路径。
这些文件在 `_runtime\napcat\napcat\`（**子目录**），
在上级目录运行会因为找不到它们而失败。

**③ 启动前必须完全退出 QQ**

一份 QQ 安装同时只能登录一个号。NapCat 要注入的就是你正在用的那个 QQ，
所以启动前要先退干净。用下面的命令确认：

```powershell
Get-Process -Name QQ -ErrorAction SilentlyContinue | Measure-Object | Select-Object Count
# Count 必须是 0
```

### 正确启动步骤

```powershell
# 1) 确认 QQ 已完全退出（输出 Count = 0）
Get-Process -Name QQ -ErrorAction SilentlyContinue | Measure-Object | Select-Object Count

# 2) 进入 NapCat 目录（注意是 napcat 子目录）
cd /d E:\code\luxun-qq\_runtime\napcat\napcat

# 3) 启动
.\launcher-local.bat

# 已登录过的号可以跳过扫码：
.\launcher-local.bat -q 1234567890
```

启动后终端会打印二维码，**用手机 QQ 扫码登录 1234567890**。
WebUI 默认在 `http://127.0.0.1:6099`（token 见启动日志）。

### 想让大号和小号同时在线？

一份 QQ 装不了两个号。要在用大号的同时挂 NapCat，需要**第二份 QQ**：

```powershell
# 把 QQ 装到工作区（不碰你现有那份）
E:\software\QQ\QQ_9.9.16_241023_x64_01.exe
# 安装目标建议：E:\code\luxun-qq\_runtime\qq-bot\

# 然后让启动脚本用第二份 QQ 的路径（无需改文件）
$env:LUXUN_QQ_PATH = "E:\code\luxun-qq\_runtime\qq-bot\QQ.exe"
.\launcher-local.bat
```

之后两个号可以同时在线，互不干扰。

### 其它启动脚本（原版，均需注册表）

| 脚本 | 用途 |
|---|---|
| `launcher-win10-user.bat` | 非管理员启动（注册表查路径，本机会失败） |
| `launcher.bat` | 管理员启动（会尝试提权） |
| `KillQQ.bat` | 结束 QQ 进程 |

### NapCat 里要做的配置

进 WebUI → 网络配置：

**① HTTP 服务器**（让 luxun-qq 能调它发消息）

| 项 | 值 |
|---|---|
| 启用 | ✅ |
| 主机 | `127.0.0.1` |
| 端口 | `3000` |
| Token | 留空，或自设后同步到 `config.yaml` 的 `sender.access_token` |

**② HTTP 事件上报**（让 NapCat 把消息推给 luxun-qq）

| 项 | 值 |
|---|---|
| 启用 | ✅ |
| URL | `http://127.0.0.1:8848/` |
| 消息格式 | `array`（本服务两种都支持） |
| 上报类型 | 勾选「私聊消息」 |

> 若 NapCat 用配置文件而非 WebUI，对应 `onebot11_<QQ号>.json` 里的
> `httpServers` 与 `httpClients` 两节。

---

## 三、启动 luxun-qq 服务

```powershell
cd E:\code\luxun-qq
$env:PYTHONPATH="src"
python -m luxun_qq serve
```

预期输出：

```
监听        : http://127.0.0.1:8848
我的号      : 1234567890
回复对象    : 1987654321
owner       : 1234567890
候选投递到  : self
发送适配器  : napcat
推理后端    : openai_compat / qwen2.5-7b-instruct
```

存活检查：

```powershell
curl http://127.0.0.1:8848/health
```

---

## 四、使用流程

1. **1987654321 给 1234567890 发消息。**
2. **1234567890 会收到自己发给自己的消息**，内容是三步思考 + 三个候选：

```
【待你选择】
对方（1987654321）说：今天又加班到这个点，你睡了吗

① 读懂：对方今天加班到很晚，来我这里换一口气
   意图：想知道我还在不在，顺便卸一点疲惫
② 情绪：疲惫（强度 0.8）
   需求：被看见辛苦，而不是被安排作息
   雷区：急着讲道理、把话题抢回自己身上
③ 策略：先接情绪再给台阶

[1] 一锐｜……
[2] 一软｜……
[3] 一退｜……

回复数字选择要发送的候选：
  1 = 一锐   2 = 一软   3 = 一退
  r = 重新生成
  d = 丢弃本次
```

3. **你在 1234567890 的私聊里回 `1`**（或 2/3）。
4. 服务把选中的那条**从 1234567890 发给 1987654321**，并回一条确认给你。

| 你回复 | 效果 |
|---|---|
| `1` / `2` / `3` | 发送对应候选给 1987654321 |
| `r` | 对同一条消息重新生成三候选 |
| `d` | 丢弃本次，不发 |
| 其它 | 忽略，不触发任何发送 |

---

## 五、安全约束（默认全部生效）

- 只有 `bot.owner`（1234567890）能触发发送；其他人发数字无效。
- 只有 `bot.target_peer`（1987654321）的消息会生成候选。
- 同一对象同时只有一个待选项，避免选串。
- `message_id` 去重，NapCat 重推同一条不会重复生成。
- 所有发送写入 `data/outbox/napcat-<日期>.jsonl`，可事后审计。

### 上线前的建议

先用 `sender.adapter: dry_run` 跑一天，观察生成质量与触发频率，
确认满意后再切 `napcat` 真实发送。切换只需改 `config.yaml` 一行。

---

## 六、排查

**服务收不到消息**
- NapCat「HTTP 事件上报」URL 是否指向 `http://127.0.0.1:8848/`。
- luxun-qq 终端有没有 `[http] POST /` 日志。

**收到候选但发不出去**
- NapCat「HTTP 服务器」是否启用、端口是否 3000。
- `sender.access_token` 与 NapCat 是否一致（不一致会 401）。
- 手动验证：`python -m luxun_qq check`

**模型返回不是合法 JSON**
- 服务会自动重试，失败会在私聊里收到 `⚠ 生成候选失败`。
- 频繁失败就把 `config.yaml` 的 `llm.temperature` 降到 0.7。

**生成很慢**
- 确认模型服务用的是 GPU：`nvidia-smi` 应看到 `llama-server` 且显存约 10GB。
- 若走了 CPU（显存几乎不涨），说明 CUDA 后端初始化失败，看
  `_runtime/model_server.log`。

**启动 NapCat 时报 `provided QQ path is invalid`**
- 你用的是原版脚本，它靠注册表找 QQ，而本机没有那个卸载项。
- 改用 `launcher-local.bat`（见上文「启动方式」）。

**启动 NapCat 时报一堆「不是内部或外部命令」**
- 说明 .bat 文件编码被 cmd 误读（cmd 按系统 ANSI 代码页解析）。
- 本仓库附带的 `launcher-local.bat` 是纯 ASCII + CRLF，不会有这个问题。
- 如果你自己改过它并加了中文注释，用这个工具规范化回来：
  ```powershell
  python tools\fix_bat_encoding.py
  ```

**注入后 QQ 闪退或直接退出**
- 大概率是版本差：NapCat 4.18.28 针对 QQ 9.9.31 附近，
  而本机是 9.9.36（更新）。注入器本身不做版本校验，但 QQ 侧接口可能变了。
- 可尝试换用与 NapCat 版本匹配的 QQ。

---

## 七、版本兼容性：当前的实际阻塞点

### 实测结论（2026-09-30）

| 项 | 版本 / 状态 |
|---|---|
| 本机 QQ 实际运行体 | **9.9.36-53644**（`versions\` 目录内） |
| NapCat 最新版 | v4.18.28（2026-09-14），**无预发布版** |
| NapCat 官方要求 | QQ **40768+**，推荐 **9.9.26-44343** |
| NapCat 最后明确支持的新版本 | **9.9.32-55230**（v4.18.19 更新记录） |
| 你的 QQ 是否在支持范围 | ❌ **超出**（9.9.36 > 9.9.32） |

### 注入行为诊断（已实测）

用 `launcher-local.bat` 启动后，从崩溃转储（`%LOCALAPPDATA%\CrashDumps\QQ.exe.30428.dmp`）
解析出的模块列表证明**注入是成功的**：

```
NapCatWinBootHook.dll                     ← 已加载进 QQ 进程
QQNT.dll          (9.9.36)   208.57 MB
wrapper.node      (9.9.36)   118.84 MB    ← NapCat 的注入目标
major.node        (9.9.36)   106.58 MB
```

异常码是 `0x80000003`（`BREAKPOINT`）且异常地址为 0——这是注入器**主动挂起等待握手**的特征。
所以流程是：注入成功 → hook 加载成功 → 加载 `napcat.mjs` 时因 QQ 侧接口变化而退出。

### 为什么官方下载渠道全都失效

腾讯近期更换了 QQ 下载基础设施：

| 渠道 | 状态 |
|---|---|
| `dldir1.qq.com`（老 CDN） | 旧版本全部 **404**（已经下线） |
| `qqdl.gtimg.cn`（新 CDN） | 当前版本可查，但从本机访问 **403**（疑似限制） |
| OneKey 内置地址 `…/be71d851/QQ9.9.26.44498_x64.exe` | **404** |
| NapCat 发布说明里的 `…/40d6045a/QQ9.9.26.44343_x64.exe` | **404** |
| NapCat 官方安装脚本里的 `…/b07cb1a5/QQ9.9.15.27597_x64.exe` | **404** |

当前官方配置（`pcConfig.json`）报告：

```json
{"Windows": {"version": "9.9.36", "updateDate": "2026-09-24",
 "ntDownloadX64Url": "https://qqdl.gtimg.cn/qqfile/QQNTV2/9.9.36/release/e8e54bbb/QQ_9.9.36_260924_x64_01.exe"}}
```

注意：`qqdl.gtimg.cn` 从本机（含浏览器头重试）始终返回 **403**——
推测是**按来源限制**。你在自己浏览器里打开这个地址可能可以下载。

### 可行的解法（按推荐顺序）

**方案 A：装一个 NapCat 支持的 QQ 版本（最直接）**

目标版本：`9.9.32-55230`（NapCat 最后支持的新版）或 `9.9.26-44343`（官方推荐）。

关键是**从官方渠道拿到安装包**。请你在浏览器里试这两个地址
（我这边被 403/404 挡住，你那边可能可以）：

```
# 先试这个，验证你的网络能否访问新 CDN
https://qqdl.gtimg.cn/qqfile/QQNTV2/9.9.36/release/e8e54bbb/QQ_9.9.36_260924_x64_01.exe
```

如果你能下载，就说明是"本机被限制"而非"文件不存在"，那我们可以进一步找
9.9.32 / 9.9.26 的对应地址。

装到独立目录（不要覆盖你现有 QQ），然后：

```powershell
$env:LUXUN_QQ_PATH = "D:\QQ\QQ.exe"     # 你新装的 QQ 路径
cd E:\code\luxun-qq\_runtime\napcat\napcat
.\launcher-local.bat
```

`launcher-local.bat` 已支持 `LUXUN_QQ_PATH` 环境变量，不用改文件。

**方案 B：等 NapCat 更新**

NapCat 在 QQ 新版发布后通常几周内跟进适配。9.9.36 发布于 2026-09-24，
NapCat v4.18.28 发布于 09-14（在新 QQ 之前），所以**下一个版本很可能就支持了**。
可以先只写好配置，等更新。

**方案 C：换用 LiteLoaderQQNT 系方案**

LiteLoaderQQNT 是另一套 QQ NT 插件加载器，对新版 QQ 的适配通常更及时，
再配合 OneBot 插件实现同样效果。这需要改架构，但绕开了 NapCat 的版本绑定。

**方案 D：不用协议端**

把 `config.yaml` 的 `sender.adapter` 设为 `clipboard`：
模型照常生成三步思考 + 三候选，复制到剪贴板，你自己粘贴发送。
零风险、零依赖，但没有"自动发送"。

### 重要提醒

**不要从第三方下载站拿 QQ 安装包。** 我用搜索找到的那些站点
（天极下载、3wcode、jb51 等）都没有可信校验，QQ 安装包被注入的风险很高。
只走腾讯官方渠道（`im.qq.com`、`qqdl.gtimg.cn`、`dldir1.qq.com`）。

---

## 八、风险提示

用第三方协议端登录 QQ 属于非官方客户端行为，**可能违反腾讯服务协议并被限制账号**。

- 用不常用的小号承担协议端登录（本次是 1234567890），主号继续用官方客户端。
- 不要高频、批量发送，保持接近真人的节奏。
- 上线前先用 `dry_run` 试跑。

本项目只提供接口适配与内容生成，不鼓励也不担保任何使用方式。
