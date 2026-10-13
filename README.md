# luxun-qq

> **AI 开发声明：本项目自身全部代码均由 AI 开发。** 项目自身源码以 MIT 许可证开源，允许在保留版权声明和许可声明的前提下使用、修改、分发及商用，详见 [LICENSE](LICENSE)。第三方组件不属于此声明范围；内置版中的 NapCat 禁止商业用途，QQ 遵循腾讯条款。

> **Windows 桌面版**：支持大模型 API、本地服务或 GGUF 文件；在应用内查看、编辑和选择候选回复，QQ 只发送最终内容。可打包内置 QQ/NapCat 的单 EXE，不内置推理引擎和模型权重；提供无需 QQ/模型环境的离线演示。内置组件未在开发机启动，真实登录和收发需在目标电脑验收。使用与打包说明见 [桌面版指南](docs/DESKTOP.md)。

一个 QQ 自动回复助手：**读懂对方 → 分析情绪 → 给出策略**，写出三个候选回复，你选一个，它自动发出去；也可以让它**全权托管**自动回。

> **启动步骤见 [启动指南.md](启动指南.md)。**
>
> 设计取向：不训练模型，直接调**本地语言模型**或**免费大模型 API**。
> 你本人的说话风格通过「风格样本」注入，不需要几千条聊天记录也能见效。

内置两种回答角色（`config.yaml` 里 `persona` 切换）：

| persona | 三个候选 | 说明 |
|---|---|---|
| `emotion_master_male`（默认） | 情商线 / 直球线 / 稳一手 | **情感大师 · 男方视角**：站在男方立场替你回消息 |
| `luxun` | 一锐 / 一软 / 一退 | **鲁迅式锐评腔**：冷眼热心，句子短，落点暖 |

---

## 它长什么样

默认人格（情感大师 · 男方视角）的实际输出：

```
$ python -m luxun_qq plan "今天上班好累啊，被领导骂了" --peer 1987654321

┌─ 第一步 · 读懂对方 ──────────────────────────────────────────
│ 字面：今天上班好累啊，被领导骂了
│ 意图：表达工作的疲惫和被批评后的委屈，想找人接住情绪。
│ 事实：对方今天上班很累
│ 事实：对方被领导批评了
├─ 第二步 · 分析情绪 ──────────────────────────────────────────
│ 情绪：沮丧（强度 0.8）
│ 需求：被理解和支持，而不是被讲道理。
│ 雷区：说教、把话题抢回自己身上
├─ 第三步 · 回复策略 ──────────────────────────────────────────
│ 策略：先接情绪，再给一个具体的落点
│ 腔调：温和、理解，句子短，不说教
└─ 候选回复 ────────────────────────────────────────────────
  [1] 情商线｜听起来你今天挺辛苦的，被骂的时候心情糟透了吧。
  [2] 直球线｜领导有时候确实会让人烦躁，需要找个出口发泄一下。
  [3] 稳一手｜晚上想吃什么？我今晚给你做顿好吃的。
```

三个候选刻意做出**真实差异**，而不是同义改写。

切到 `persona: luxun` 则会变成**鲁迅式锐评腔**：

```
  [1] 一锐｜这个点还在耗着，公司该给你发两块碑。先去洗把脸，我在这儿。
  [2] 一软｜我醒着。挑今天最堵心的那一件讲，讲完就睡，剩下的交给明天。
  [3] 一退｜世上的班是加不完的，人的血是有限的。今晚先把自己留下来。
```

---

## 自动回复：接 QQ（OneBot / NapCat）

> **启动步骤请看 [启动指南.md](启动指南.md)** —— 三个组件（模型服务 / NapCat / 回复服务）
> 分别怎么起、怎么验证、出问题怎么查，都写在那里。下面是速览。

```powershell
# 1) 启动本地模型（常驻守护，RTX 5080 约占用 8GB 显存）
cd E:\code\luxun-qq
$env:PYTHONPATH="src"
python tools\model_server.py supervise

# 2) 启动 NapCat（另开终端，必须先于第 3 步）
cd C:\Users\<你的用户名>\qq930\NapCat
$env:LUXUN_QQ_PATH = 'C:\Users\<你的用户名>\qq930\QQ.exe'
.\launcher-napcat.bat

# 3) 启动回复服务（另开终端）
cd E:\code\luxun-qq
$env:PYTHONPATH="src"
python -m luxun_qq serve-ws
```

用的是 **WebSocket 模式**（`serve-ws`）：服务主动连 NapCat 的 `ws://127.0.0.1:3001`，
收事件与发消息共用一条连接。旧的 `serve`（HTTP 事件上报）在本机环境下只推空心跳，已弃用。

工作方式：NapCat 用 **1234567890** 登录 → 监听白名单里的人发来的消息
→ 生成三步思考 + 三候选 → **把候选发给你自己（1234567890）** →
你回 `1`/`2`/`3` → 由 1234567890 发给那个人。

### 回复范围：白名单

名单文件 **`data/whitelist.txt`**，一行一个 QQ 号，`#` 是注释：

```
1987654321
# 2222222222
```

**改完存盘立即生效，不用重启服务**（按文件 mtime 热加载）。

| 目的 | 怎么做 |
|---|---|
| 加人 / 删人 | 编辑 `data/whitelist.txt` |
| 回复所有人 | `config.yaml` 里 `bot.target_mode: "all"`（需重启） |
| 谁都不回 | 名单全部注释掉（服务会提示一次「名单为空」） |

配置项：

```yaml
bot:
  target_mode: "whitelist"        # "whitelist" 只回名单里的人；"all" 谁都回
  target_peer: ""                 # 额外的号，逗号分隔（与名单文件取并集）
  whitelist_file: "data/whitelist.txt"
```

> 未显式写 `target_mode` 时会按 `target_peer` 推断：
> `"*"` 或空 → `all`，填了号 → `whitelist`，所以旧配置仍能照常用。

| 你回复 | 效果 |
|---|---|
| `1` / `2` / `3` | 发送对应候选（默认人格下是 情商线 / 直球线 / 稳一手） |
| `0` | **全权交给模型回复**，并开启自动托管——之后对方来消息直接自动回 |
| `a` | 切换持续托管（无人待选时按 = 对所有人托管） |
| `n` | 取消托管，恢复人工选择 |
| `r` | 重新生成三候选 |
| `d` | 丢弃本次 |

**多人同时来消息时**，不加号则作用于最近收到的那条；加 QQ 号可精确指定：

```
1987654321 1      ← 只发给 1987654321
2222222222 a      ← 只对 2222222222 开托管
```

每条回复发出前会**随机等待 10~30 秒**（模拟真人打字节奏），可在 `config.yaml`
的 `bot.send_delay_min` / `bot.send_delay_max` 调整。

安全约束：只有配置的 `bot.owner` 能触发发送（owner 自己的消息永远不生成候选）；
只有白名单里的号会生成候选；每个对象各自只有一个待选项，互不干扰；
所有发送写入 `data/outbox/` 审计。

---

## 仓库里没有大文件（先看这一节）

运行本项目需要约 **14GB** 的本地文件，但仓库里**只有源码**（约 0.6MB）。
原因很硬：GitHub 限制单文件最大 **100MB**，而这些文件里有 14 个超过它
（最大的 GGUF 模型 **3.8GB**），Git LFS 免费额度（1GB）也装不下。

缺的东西用脚本补齐：

```bash
python tools/setup.py --check     # 先看缺什么
python tools/setup.py --all       # 下载模型 / NapCat / QQ 安装包
```

| 缺什么 | 体积 | 来源 | 能否自动下载 |
|---|---|---|---|
| GGUF 模型 | 4.4GB | ModelScope（国内直连，约 50MB/s） | ✅ `--model` |
| NapCat | 111MB | GitHub Releases | ✅ `--napcat`（自动走加速代理） |
| QQ 9.9.30 安装包 | 278MB | 社区版本库 | ✅ `--qq` |
| 推理后端 `llama-server.exe` | ~800MB | LM Studio 或官方 llama.cpp | ❌ 需手动，`--check` 会给出指引 |

推理后端拿不到固定 URL（LM Studio 的 CDN 路径会变），所以脚本只做检测与指引。
它支持三个位置自动探测，也可以用环境变量指定：

```powershell
$env:LUXUN_LLAMA_BACKEND = "D:\somewhere\llama.cpp"   # 含 llama-server.exe 即可
```

---

## 快速开始

```bash
cd luxun-qq

# 1) 装依赖（只需要这两个）
pip install pycryptodome pyyaml
#   PyYAML 可省：没有它就用 config.json

# 2) 让包可导入（三选一）
pip install -e .                    # 推荐
# 或  set PYTHONPATH=src            # Windows cmd
# 或  $env:PYTHONPATH="src"         # PowerShell

# 3) 先离线确认整条链路是通的（不需要任何模型和网络）
python -m luxun_qq check
python -m luxun_qq plan "在吗？借我两千块钱急用"
```

`check` 会告诉你配置、推理后端连通性、发送适配器状态。

### 配置：模板 + 本地覆盖

仓库里的 `config.yaml` 是**打码过的模板**（占位号 `1234567890` 之类）。
你自己的账号、路径写在 `config.local.yaml`，它已被 `.gitignore` 忽略：

```yaml
# config.local.yaml —— 只写与模板不同的部分，会深度合并覆盖
me: "你的QQ号"
sender:
  self_id: "你的QQ号"
bot:
  owner: "你的QQ号"
```

`Config.load()` 会先读 `config.yaml`，再叠加 `config.local.yaml`（同名字段以后者为准）。
所以换机器只改这一个文件，不用动仓库里的模板。

同理，白名单也是"示例 + 本地真实文件"：

```powershell
copy data\whitelist.example.txt data\whitelist.txt
```

---

## 选择推理后端

改 `config.yaml` 的 `llm.backend`，或直接用环境变量 `LUXUN_LLM_BACKEND`。

### A. `echo` —— 离线自检（默认）

不联网，返回内置的中文示例回复。**用来验证流程，不要用来真聊天。**

### B. `local_ollama` —— 本地模型

```bash
# 安装 Ollama 后
ollama pull qwen2.5:7b        # 有独立显卡时
ollama pull qwen2.5:3b        # 纯 CPU / 显存小（本机是纯 CPU，建议这个或更小）
ollama serve                  # 默认监听 127.0.0.1:11434
```

```yaml
llm:
  backend: local_ollama
  model: "qwen2.5:3b"
  base_url: "http://127.0.0.1:11434"
```

> 纯 CPU 推理速度有限：3B 模型每条回复大约数秒到十几秒；7B 会明显更慢。
> LM Studio 也能用——它自带 OpenAI 兼容接口，走下面的 `openai_compat`。

### C. `openai_compat` —— 免费/付费 API（推荐）

任何 OpenAI 兼容接口都能直接用。填 `base_url` + `api_key`：

```yaml
llm:
  backend: openai_compat
  model: "deepseek-chat"
  base_url: "https://api.deepseek.com"
  api_key: ""            # 建议用环境变量，别写进文件
```

```powershell
$env:LUXUN_LLM_API_KEY="sk-xxxx"
```

常见可选服务（都提供 OpenAI 兼容端点）：

| 服务 | base_url 示例 | 备注 |
|---|---|---|
| DeepSeek | `https://api.deepseek.com` | 便宜，中文强 |
| 智谱 GLM | `https://open.bigmodel.cn/api/paas/v4` | 有免费额度 |
| 阿里通义 | `https://dashscope.aliyuncs.com/compatible-mode/v1` | 有免费额度 |
| SiliconFlow | `https://api.siliconflow.cn/v1` | 部分模型免费 |
| Groq | `https://api.groq.com/openai/v1` | 免费额度，速度极快 |
| OpenRouter | `https://openrouter.ai/api/v1` | 聚合，含免费模型 |

> 免费额度、限流和政策随时会变，注册前请自行到各家官网确认；
> 建议先只充最少量的钱，并设置用量上限。

---

## 让回复像「你本人」

风格的来源是 `personas.py` 里的**回答角色**（默认「情感大师 · 男方视角」，可切「鲁迅式锐评」）
\+ 你自己的**说话样本**。角色决定腔调与视角，样本决定「像不像你本人在打字」。

**最重要的一步**：把 `data/my_style_sample.txt` 换成你真实发过的消息（20-100 条就够），
保留原始标点、错别字、emoji 和断句。模型会照着这个语感说话。

```
行吧。
也行，随你。
这个我早就说过了。
你说得都对，然后呢。
```

也可以写成对话片段（`对方：…` / `我：…`）。

如果你之后成功导出了完整的 QQ 聊天记录，还能进一步生成量化风格画像与检索索引：

```bash
python -m luxun_qq style --corpus data/corpus/me_1987654321.jsonl
```

---

## 真的把消息发出去

发送层是**可插拔**的，改 `config.yaml` 的 `sender.adapter`：

| 适配器 | 说明 | 风险 |
|---|---|---|
| `dry_run` | **默认**。不真发，打印并写入 `data/outbox/` | 无 |
| `clipboard` | 复制到剪贴板，你自己粘贴发送 | 无 |
| `webhook` | POST 到你的中转服务 | 无 |
| `napcat` | 通过 NapCat / LLOneBot 的 OneBot 11 HTTP 接口真实发送 | **见下** |

### 接入 NapCat（真实发送）

NapCat 是基于 NTQQ 的无头协议端，提供 OneBot 11 接口。
先在 NapCat 里登录，开启 **HTTP 服务**（默认端口 3000），然后：

```yaml
sender:
  adapter: napcat
  base_url: "http://127.0.0.1:3000"
  access_token: ""          # 与 NapCat 配置一致；建议用 LUXUN_SENDER_TOKEN
  self_id: "1987654321"
```

```bash
python -m luxun_qq check                        # 会尝试 get_login_info
python -m luxun_qq send --peer 123456 --text "测试"
```

⚠️ **风险提示**：使用第三方协议端接入 QQ 可能违反腾讯的服务协议，存在账号被限制的风险。
请自行评估，优先在测试号上验证；本项目只提供接口适配，不鼓励也不担保任何使用方式。

### 不装 NapCat 也能验证发送链路

仓库自带一个假 NapCat，用来端到端验证：

```bash
python -m luxun_qq.senders.echo_server 3001
# 另一个终端
python -m luxun_qq send --peer 123456 --text "hi"
# 需要先把 config.yaml 的 sender.base_url 指到 127.0.0.1:3001
```

---

## 命令一览

| 命令 | 作用 |
|---|---|
| `python -m luxun_qq plan "对方的话"` | 生成三步思考 + 三个候选（不发送） |
| `python -m luxun_qq plan "…" --send` | 生成后交互选择并发送 |
| `python -m luxun_qq plan --file msg.txt --json` | 从文件读入，附带 JSON 输出 |
| `python -m luxun_qq chat` | 交互式连续对话（`:peer <qq>` 切换对象） |
| `python -m luxun_qq check` | 自检配置 / 后端 / 发送适配器 |
| `python -m luxun_qq send --peer <qq> --text "…"` | 直接发送，不经过模型 |
| `python -m luxun_qq style --corpus <jsonl>` | 从导出语料生成风格指南与统计报告 |

选择候选时的交互键：`1/2/3` 选定 → 发送；`r` 重新生成；`e` 自己改写；
回车放弃；`q` 退出。

---

## 项目结构

```
luxun-qq/
├── config.yaml                 配置（唯一权威模板）
├── pyproject.toml
├── data/
│   ├── my_style_sample.txt     ★ 换成你自己的说话样本
│   ├── whitelist.txt           ★ 回复白名单（一行一个 QQ 号，改动即时生效）
│   ├── outbox/                 发送审计记录（每次发送都落盘）
│   └── corpus/                 风格索引与统计（可选）
├── src/luxun_qq/
│   ├── personas.py             回答角色注册表（情感大师 / 鲁迅锐评）
│   ├── persona.py              人格提示词与风格样本加载
│   ├── engine.py               三步思考引擎 + 三候选解析
│   ├── llm.py                  echo / ollama / openai 兼容 三种客户端
│   ├── service.py              HTTP 事件服务 + 审批状态机 + 自动托管
│   ├── service_ws.py           WebSocket 事件服务（推荐接入方式）
│   ├── wsclient.py             零依赖 WebSocket 客户端（手写 RFC 6455）
│   ├── cli.py                  命令行入口
│   ├── schema.py               数据契约
│   ├── config.py               配置加载（支持环境变量覆盖）
│   ├── corpus.py               语料配对、风格统计、轻量检索（可选）
│   ├── style.py                风格指南渲染（可选）
│   ├── senders/                可插拔发送层 + 假 NapCat
│   └── qqdb/                   QQ 数据库解密与导出（可选）
├── tools/                      独立小工具（不属于主流程）
│   ├── scan_qq_memory_key.py   扫描 QQ 进程内存找 SQLCipher 密钥
│   └── watch_qq_memory_key.py  同上，多轮持续监控版
└── tests/                      pytest 测试 + 开发冒烟脚本
```

---

## 测试

```bash
python tests/test_luxun_qq.py     # 单元测试：22 项，纯标准库，无需 pytest
python tests/smoke_dev.py         # 端到端冒烟：5 个场景走完三步思考 -> 三候选 -> 发送
python -m pytest tests -q         # 装了 pytest 也能跑同一批用例
```

测试覆盖：
- SQLCipher 密钥校验、解密往返（真实 SQLite → 加密 → 解密 → 逐页比对）、错误密钥必须被拒绝
- 极简 protobuf 文本抽取（含脏数据不抛异常）
- JSON 抽取容错（代码块包裹、前后夹带说明、字符串内含 `}`）
- 三步思考解析的宽松形态与缺失字段兜底、空消息报错、降级计划
- `echo` 后端 8 类场景的路由正确性（防止「在吗」抢走具体话题）与候选唯一性
- 发送层：审计落盘字段、候选选择与越界、未知适配器、连不上时的可读报错
- 风格样本加载（逐行 / 对话两种格式）

---

## 可选：从本地 QQ 数据库导出聊天记录

主流程**不需要**这一步。只有当你想要一份完整风格画像（而不是手工样本）时才需要。

QQ NT 把聊天记录存在 SQLCipher 4 加密的 `nt_msg.db` 里，文件开头还有 1024 字节的
`QQ_NT DB` 自定义头。要读它需要两样东西：

1. **数据库文件**（本机已有）
2. **密钥**（16 字节 ASCII，每个账号不同，只能从运行中的 QQ 进程里取）

本仓库内置了**不依赖 sqlcipher3 编译包**的纯 Python 解密实现
（`qqdb/crypto.py`，已通过加密→解密的往返测试），以及最小 Protobuf 解析器
（`qqdb/export.py`，用于从消息体里抽文本）。

**关于密钥**：我尝试过直接扫描运行中 QQ 进程的内存来提取密钥，未命中——
连续扫描 5 个 QQ 进程、每轮约 3.2 万个候选，8 轮都没有命中，
说明该密钥**不以明文常驻内存**。相关脚本保留在
[tools/watch_qq_memory_key.py](tools/watch_qq_memory_key.py)，你可以自己复现这个结论。

可行途径是调试挂载 QQ 进程、在 `nt_sqlite3_key_v2` 处断点读取密钥，
社区工具见 [QQBackup/qq-win-db-key](https://github.com/QQBackup/qq-win-db-key) 与
[QQDecrypt 文档](https://qqbackup.github.io/QQDecrypt/)。
注意：这些做法会调试/注入 QQ 进程，可能触发安全检测，请自行评估风险。

比较稳妥的替代方案：用 QQ 自带的「导出消息记录」，或
[qq-chat-exporter](https://github.com/shuakami/qq-chat-exporter/) 导出成文件再解析。

拿到密钥后：

```bash
# 1) 解密（输出明文 SQLite）
python -c "from luxun_qq.qqdb import crypto as c; \
  c.decrypt_to_plaintext('nt_msg.db','nt_msg_plain.db','你的16位密钥', \
  c.CipherParams(header_bytes=1024))"

# 2) 导出为归一化 JSONL
# 见 qqdb/export.py 的 export_c2c()
```

若你的 QQ 版本参数不同（页大小 / KDF 迭代次数 / HMAC 算法），
改 `CipherParams` 即可，无需改代码。

---

## 已知限制

- **三步思考依赖模型质量**：`echo` 后端的思考内容是预设的（内置 8 类场景），
  真实质量取决于你选的模型。
- **反语识别天然困难**：情绪分析是启发式 + 模型判断，遇到反讽可能判错。
  检测这类失误的好办法：把真发出去的话记下来，回头看看哪条判错了。
- **免费 API 会限流**：额度、速率随时变化，请以各家官网为准。
- **QQ 聊天记录解密（可选能力）存在数据保留区限制**：
  本仓库的纯 Python 解密实现不解析 SQLCipher 的**页尾保留区**
  （每页 IV+HMAC 之后的字节，页 1 尾部 96 字节含 `sqlite_master` 记录）。
  实测后果：解出来的库 `pragma integrity_check` 会报 `malformed database schema
  - orphan index`，**不能直接当 SQLite 库打开**，但页内数据区是逐字节正确的，
  可用于纯文本提取（本项目只需要文本）。
  要拿到结构完整、可直接打开的库，请用原生 `sqlcipher3` 或官方工具：
  ```bash
  pip install sqlcipher3-binary        # 部分 Python 版本暂无 wheel
  ```
  测试里 `test_decrypt_roundtrip_preserves_data` 显式断言了这一限制，
  哪天保留区被正确保留，该断言会失败并提醒你把测试升级为完整 integrity_check。
- **合规**：请只用于你本人有权处理的账号与数据。

---

## License

本项目采用 [MIT License](LICENSE) 开源，不再限制为个人学习与自用。

本项目全部代码均由 AI 开发。软件按“原样”提供，不作任何明示或暗示的担保。
第三方依赖、模型及 QQ/NapCat 等外部组件仍遵循各自的许可证与服务条款。
使用者需自行承担因接入第三方 QQ 协议端而产生的一切后果。
