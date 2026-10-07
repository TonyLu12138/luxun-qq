# -*- coding: utf-8 -*-
"""配置加载：config.yaml -> Config 对象，支持环境变量覆盖。

依赖 PyYAML（可选）：装了就读 yaml，没装就要求 config.json 或直接用默认值。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

try:  # pragma: no cover
    import yaml  # type: ignore

    _HAS_YAML = True
except Exception:  # pragma: no cover
    _HAS_YAML = False


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = ROOT / "config.yaml"
DATA_DIR = ROOT / "data"
RAW_DIR = DATA_DIR / "raw"
CORPUS_DIR = DATA_DIR / "corpus"
SAMPLE_DIR = DATA_DIR / "sample"

#: 记忆库的默认位置。
#:
#: 单独提成一个模块级变量（而不是直接写进 dataclass 默认值）是为了让测试
#: 能真正改掉它：dataclass 生成 __init__ 时会把默认值固化成参数默认值，
#: 之后再改类属性是无效的。MemoryConfig.__post_init__ 在这里读当前值，
#: 所以测试只要覆盖这个变量就能把所有"未指定路径"的实例引到临时目录，
#: 从而杜绝污染用户真实的 data/memory.db。
DEFAULT_MEMORY_DB = DATA_DIR / "memory.db"


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """递归合并两个配置字典，``override`` 优先。

    只做一层"字典套字典"的递归，列表整体替换（不合并元素）——
    配置里列表语义都是"整份替换"，逐项合并反而会让人意外。
    """
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


@dataclass
class LLMConfig:
    """推理后端配置。三种 backend：local_ollama / openai_compat / echo。"""

    backend: str = "echo"
    model: str = "qwen2.5:7b"
    base_url: str = "http://127.0.0.1:11434"
    api_key: str = ""
    temperature: float = 0.9
    top_p: float = 0.9
    max_tokens: int = 512
    timeout: int = 120
    #: 两段式生成：先自由写候选（语气优先），再单独结构化（格式优先）。
    #: 7B 级模型上语气明显更好；关掉则退回"一次调用出三步思考 JSON"。
    two_stage: bool = True
    #: 语气阶段的尝试次数。7B 模型在高温下方差大，多试几次取最好的一条，
    #: 比反复调 prompt 更可靠。1 = 不重试。
    voice_attempts: int = 3


@dataclass
class RetrieveConfig:
    """风格参考（可选）。

    两种来源，可同时启用：
      - style_sample：手工整理的「我平时怎么说话」纯文本（推荐，零依赖）
      - index_path  ：从导出的聊天语料构建的检索索引（需要先导出聊天记录）
    """

    enabled: bool = True
    top_k: int = 6
    min_score: float = 0.05
    index_path: str = str(CORPUS_DIR / "style_index.jsonl")
    stats_path: str = str(CORPUS_DIR / "style_stats.json")
    style_sample: str = str(DATA_DIR / "my_style_sample.txt")
    max_reply_chars: int = 120


@dataclass
class MemoryConfig:
    """长期记忆：把对话持久化，回复前召回相关旧内容。

    分两层：
        * 消息层——全部对话原文，按关键词召回"很久以前说过什么"
        * 事实层——从对话里抽出的长期事实（生日、工作、偏好、约定），
          每轮都注入，因为它们对任何话题都重要
    """

    #: 总开关。关掉则退回"只有内存里的最近 N 轮"
    enabled: bool = True
    #: SQLite 文件路径（相对项目根目录）。留空表示用 DEFAULT_MEMORY_DB，
    #: 在 __post_init__ 里解析——这样默认值可被测试覆盖。
    db_path: str = ""
    #: 短期窗口：最近多少轮直接进上下文
    recent_limit: int = 16

    #: **喂给提示词**的历史轮数。刻意比 recent_limit 小很多。
    #:
    #: 为什么：实测发现，把 10 轮原文历史一起塞进提示词时，7B 模型会
    #: 被最近聊过的话题"锚住"——对方换了个无关话题（比如「今天好累」），
    #: 它还是硬把上一轮的面试扯出来。收窄到几轮后，它转而依赖
    #: 相关性召回的记忆，表现明显更好（实测「吃火锅」能正确想到对方不能吃辣）。
    #:
    #: 所以分工是：**少量历史负责对话连贯，记忆库负责长期信息**。
    prompt_history_limit: int = 4
    #: 长期召回：按关键词找回多少条较早的消息
    recall_limit: int = 6
    #: 总是注入的"核心"事实条数（对任何话题都成立的类型）
    core_fact_limit: int = 5
    #: 被认为"核心"的事实类型。
    #: 偏好也放进来——因为关键词召回抓不到语义关联：
    #: 对方说「吃火锅」，字面上跟「不能吃辣」没有任何共同词，搜不到。
    core_fact_kinds: tuple[str, ...] = ("身份", "关系", "偏好")
    #: 按当前消息关键词额外召回的事实条数
    relevant_fact_limit: int = 4
    #: 兼容旧配置：若设置则作为 core+relevant 的总上限参考（不再直接使用）
    fact_limit: int = 20
    #: 是否让模型从对话里抽取事实（后台线程做，不阻塞回复）
    extract_facts: bool = True
    #: 每积累多少条消息抽一次事实
    extract_every: int = 6
    #: 事实抽取用的温度（要稳，不能发挥）
    extract_temperature: float = 0.2

    def __post_init__(self) -> None:
        # 延迟解析默认路径：见 DEFAULT_MEMORY_DB 的注释
        if not str(self.db_path or "").strip():
            self.db_path = str(DEFAULT_MEMORY_DB)


@dataclass
class SenderConfig:
    """发送层配置。adapter: napcat / webhook / clipboard / dry_run。"""

    adapter: str = "dry_run"
    base_url: str = "http://127.0.0.1:3000"     # NapCat OneBot11 HTTP
    access_token: str = ""
    self_id: str = ""                            # 我的 QQ 号
    timeout: int = 20


@dataclass
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 8848


@dataclass
class BotConfig:
    """自动回复服务配置（OneBot 11 / NapCat）。

    典型场景：NapCat 用 A 号（sender.self_id）登录，
    监听 B 号（target_peer）发来的消息并把三候选投给 owner 审批。
    """

    host: str = "127.0.0.1"
    port: int = 8848
    access_token: str = ""            # webhook 鉴权；留空则不校验

    # ---- 回复范围 ----
    #: 范围模式：
    #:   "all"        任何人的私聊都回复
    #:   "whitelist"  只回复白名单里的号（推荐）
    #:   留空则按 target_peer 推断："*"/空 → all，否则 → whitelist
    target_mode: str = ""
    #: 要回复的对象；支持逗号/空格分隔的多个号，或 "*" 表示所有人
    target_peer: str = ""
    #: 白名单文件（相对项目根目录）；一行一个 QQ 号，# 开头为注释。
    #: 按文件修改时间热加载——改完存盘立即生效，不用重启服务。
    whitelist_file: str = "data/whitelist.txt"

    # 审批人（通常就是登录 NapCat 的那个号）
    owner: str = ""
    # 候选与结果投递到哪：""=只写日志，"self"=发给自己，或指定 QQ 号
    owner_sink: str = "self"
    #: 是否打印每条上报事件的详情（排查「收到上报但没反应」时打开）
    log_events: bool = True
    #: 是否把 POST 的原始 body 也打出来（排查空 body / 格式不符时打开）
    log_raw_body: bool = True
    #: WebSocket 模式专用：NapCat 的 WS 服务器地址（留空则按 sender.base_url 推导）
    ws_url: str = "ws://127.0.0.1:3001"

    # ---- 发送延迟：模拟真人打字节奏 ----
    #: 发送前的随机等待下限（秒）。0 表示不等待。
    send_delay_min: float = 10.0
    #: 发送前的随机等待上限（秒）
    send_delay_max: float = 30.0

    # ---- 自动回复（全权托管）----
    #: 允许 owner 用 "0" / "a" 让模型全权回复
    allow_auto_reply: bool = True

    # ---- 退出时清理 ----
    #: 回复服务退出时是否顺手停掉本地模型服务（释放显存）。
    #: 默认 False：模型服务有自己的终端窗口，关那个窗口就会释放显存
    #: （supervisor 挂了 Job Object，父进程死则 llama-server 一起死）。
    #: 如果你习惯只关回复服务的窗口，把它设成 true 就能一并清掉显存。
    stop_model_on_exit: bool = False


@dataclass
class Config:
    me: str = "1987654321"
    peer: str = ""                     # 目标对话对象；留空表示全部
    #: 回答角色（见 personas.py）：emotion_master_male / luxun
    persona: str = "emotion_master_male"
    source_db: str = ""
    source_header_bytes: int = 1024
    db_key: str = ""
    sqlcipher_kdf_iter: int = 4000
    sqlcipher_kdf_sha512: bool = True
    sqlcipher_hmac_sha512: bool = True

    llm: LLMConfig = field(default_factory=LLMConfig)
    retrieve: RetrieveConfig = field(default_factory=RetrieveConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    sender: SenderConfig = field(default_factory=SenderConfig)
    server: ServerConfig = field(default_factory=ServerConfig)
    bot: BotConfig = field(default_factory=BotConfig)

    # ---- 载入 ----
    @classmethod
    def load(cls, path: Optional[str | Path] = None) -> "Config":
        """载入配置。

        默认读 ``config.yaml``，然后叠加 ``config.local.yaml``（若存在）。
        这样仓库里可以放一份**打码过**的 config.yaml 供他人参考，
        而真实账号、路径留在 config.local.yaml —— 后者已在 .gitignore 里，
        不会被提交。叠加是"深合并"，local 里的字段覆盖同名字段。
        """
        path = Path(path) if path else DEFAULT_CONFIG
        raw = cls._read_config_file(path)
        # 本地覆盖文件：与主配置同目录，文件名固定为 config.local.<后缀>
        local = path.with_name(f"{path.stem}.local{path.suffix}")
        if local.exists() and local != path:
            raw = _deep_merge(raw, cls._read_config_file(local))
        return cls.from_dict(raw)

    @staticmethod
    def _read_config_file(path: Path) -> dict[str, Any]:
        """读单个 yaml/json 配置文件，返回 dict（不存在则空）。"""
        if not path.exists():
            return {}
        text = path.read_text(encoding="utf-8")
        if path.suffix.lower() in (".yaml", ".yml"):
            if not _HAS_YAML:
                raise RuntimeError(
                    f"{path} 是 YAML，但未安装 PyYAML。请 `pip install pyyaml` "
                    f"或改用 config.json。")
            return yaml.safe_load(text) or {}
        return json.loads(text)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Config":
        cfg = cls()
        for key in ("me", "peer", "persona", "source_db", "source_header_bytes", "db_key",
                    "sqlcipher_kdf_iter", "sqlcipher_kdf_sha512", "sqlcipher_hmac_sha512"):
            if key in raw and raw[key] is not None:
                setattr(cfg, key, raw[key])
        for name, klass in (("llm", LLMConfig), ("retrieve", RetrieveConfig),
                            ("memory", MemoryConfig),
                            ("sender", SenderConfig), ("server", ServerConfig),
                            ("bot", BotConfig)):
            sub = raw.get(name) or {}
            obj = klass()
            for k, v in sub.items():
                if hasattr(obj, k):
                    setattr(obj, k, v)
            setattr(cfg, name, obj)
        cfg._apply_env()
        return cfg

    def _apply_env(self) -> None:
        """环境变量覆盖敏感项，避免密钥写进配置文件。"""
        env_map = {
            "LUXUN_DB_KEY": ("db_key", str),
            "LUXUN_LLM_BACKEND": ("llm.backend", str),
            "LUXUN_LLM_MODEL": ("llm.model", str),
            "LUXUN_LLM_BASE_URL": ("llm.base_url", str),
            "LUXUN_LLM_API_KEY": ("llm.api_key", str),
            "LUXUN_SENDER_ADAPTER": ("sender.adapter", str),
            "LUXUN_SENDER_BASE_URL": ("sender.base_url", str),
            "LUXUN_SENDER_TOKEN": ("sender.access_token", str),
            "LUXUN_SELF_ID": ("sender.self_id", str),
            "LUXUN_PEER": ("peer", str),
        }
        for env, (attr, caster) in env_map.items():
            val = os.environ.get(env)
            if not val:
                continue
            if "." in attr:
                head, tail = attr.split(".", 1)
                setattr(getattr(self, head), tail, caster(val))
            else:
                setattr(self, attr, caster(val))

    def to_dict(self) -> dict[str, Any]:
        from dataclasses import asdict

        d = asdict(self)
        if d.get("db_key"):
            d["db_key"] = "***"
        if d["llm"].get("api_key"):
            d["llm"]["api_key"] = "***"
        if d["sender"].get("access_token"):
            d["sender"]["access_token"] = "***"
        return d

    def me_dir(self) -> Path:
        return RAW_DIR / f"me_{self.me}"


# 默认配置模板的唯一权威来源是仓库根目录的 config.yaml，
# 这里不再内置副本，避免两处不一致。
DEFAULT_CONFIG_PATH = DEFAULT_CONFIG
