# -*- coding: utf-8 -*-
"""统一 LLM 客户端层：供上层三步思考引擎调用。

三种后端（对应 ``LLMConfig.backend``）：

* ``echo``          —— 完全离线，返回可预测的「鲁迅式锐评」示例，用于跑通全链路自检；
* ``local_ollama``  —— 本地 Ollama ``/api/chat``；
* ``openai_compat`` —— OpenAI 兼容接口 ``{base_url}/v1/chat/completions``
                       （DeepSeek / OpenAI / vLLM / LM Studio 等）。

设计约束：
* 只用标准库（``urllib.request``）与项目内的 dataclass 配置，不引入第三方依赖；
* ``chat_json`` 负责把模型输出里「第一个平衡的 JSON 对象」抽出来再解析，
  失败会带纠正提示重试一次，仍失败则抛 ``LLMError``。
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .config import LLMConfig

__all__ = [
    "LLMResponse",
    "LLMError",
    "LLMClient",
    "EchoClient",
    "OllamaClient",
    "OpenAICompatClient",
    "build_client",
    "extract_json_object",
    "ECHO_STEP1_READ",
    "ECHO_STEP2_EMOTION",
    "ECHO_STEP3_STRATEGY",
]

# 默认地址与模型（缺省配置里的值兜底）
DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434"
DEFAULT_OPENAI_COMPAT_URL = "https://api.openai.com"

# 解析 JSON 失败时追加的纠正指令（重试一次时使用）
RETRY_HINT = "只输出 JSON，不要任何其他文字"


class LLMError(RuntimeError):
    """LLM 调用层统一异常。所有子类实现都应抛这个异常并给出中文可读信息。"""


@dataclass
class LLMResponse:
    """一次模型调用的结果。"""

    text: str                                  # 回复正文
    raw: dict[str, Any] = field(default_factory=dict)   # 原始响应体，便于回溯
    model: str = ""
    backend: str = ""

    def __str__(self) -> str:  # pragma: no cover - 便于交互式打印
        return self.text


# --------------------------------------------------------------------------- #
# 抽象基类
# --------------------------------------------------------------------------- #
class LLMClient(ABC):
    """LLM 客户端抽象基类。"""

    backend: str = ""

    def __init__(self, cfg: LLMConfig) -> None:
        self.cfg = cfg

    # ---- 子类必须实现 ----
    @abstractmethod
    def chat(
        self,
        messages: list[dict],
        *,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        json_mode: bool = False,
    ) -> LLMResponse:
        """发起一次对话补全。``messages`` 为 ``[{"role": ..., "content": ...}]``。"""
        raise NotImplementedError

    # ---- 便捷方法 ----
    def chat_text(self, system: str, user: str, **kw: Any) -> str:
        """system + user 两段式调用，直接返回文本。"""
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        return self.chat(messages, **kw).text

    def chat_json(self, system: str, user: str, **kw: Any) -> dict:
        """要求模型返回 JSON 对象并解析成 dict。

        容错策略：
        1. 先正常请求（``json_mode`` 默认打开，由 ``kw`` 覆盖）；
        2. 解析失败则追加一句「只输出 JSON，不要任何其他文字」重试一次；
        3. 仍然失败抛 ``LLMError``。
        """
        kw.setdefault("json_mode", True)
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]

        resp = self.chat(messages, **kw)
        obj = _try_parse_object(resp.text)
        if obj is not None:
            return obj

        # 重试一次：把上一轮的输出与纠正指令一起喂回去
        retry_messages = list(messages)
        if resp.text.strip():
            retry_messages.append({"role": "assistant", "content": resp.text})
        retry_messages.append({"role": "user", "content": RETRY_HINT})

        resp2 = self.chat(retry_messages, **kw)
        obj2 = _try_parse_object(resp2.text)
        if obj2 is not None:
            return obj2

        snippet = (resp2.text or "").strip().replace("\n", " ")[:300]
        raise LLMError(
            f"模型两次都没有返回可解析的 JSON（backend={self.backend}，"
            f"model={resp2.model or self.cfg.model}）。最后一次输出片段：{snippet!r}"
        )


# --------------------------------------------------------------------------- #
# JSON 抽取工具
# --------------------------------------------------------------------------- #
def _strip_code_fences(text: str) -> str:
    """把 ```json ... ``` 或 ``` ... ``` 代码块的外壳去掉，返回内部内容。"""
    t = text.strip()
    if "```" not in t:
        return t
    # 取第一个代码块的内容（```lang\n ... \n```）
    first = t.find("```")
    body_start = t.find("\n", first + 3)
    if body_start == -1:
        return t
    end = t.find("```", body_start)
    if end == -1:
        return t[body_start + 1:]
    return t[body_start + 1:end]


def extract_json_object(text: str) -> Optional[str]:
    """从任意文本里抽出**第一个平衡的 JSON 对象**字面量。

    能处理：纯 JSON、```json 包裹、前后带解释文字、字符串里含花括号等情况。
    找不回来返回 ``None``。
    """
    if not text:
        return None

    candidate = _strip_code_fences(text)
    # 先按「大括号计数 + 字符串状态机」扫描原文，失败再扫去代码块后的文本
    for source in (candidate, text):
        found = _scan_balanced_object(source)
        if found is not None:
            return found
    return None


def _scan_balanced_object(text: str) -> Optional[str]:
    """扫描文本，返回第一个平衡 ``{...}`` 子串（跳过字符串内部的括号）。"""
    start = text.find("{")
    if start == -1:
        return None

    depth = 0
    in_str = False
    escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


def _try_parse_object(text: str) -> Optional[dict]:
    """尽力把模型输出解析成 dict；失败返回 None（不抛异常）。"""
    literal = extract_json_object(text or "")
    if literal is None:
        return None
    try:
        obj = json.loads(literal)
    except (json.JSONDecodeError, ValueError):
        return None
    return obj if isinstance(obj, dict) else None


# --------------------------------------------------------------------------- #
# 1. Echo：离线自检
# --------------------------------------------------------------------------- #
ECHO_STEP1_READ: dict[str, Any] = {
    "literal": "对方说今天又加班到这个点，问我睡了没。",
    "intent": "不是真的关心我睡没睡，是想找个人接住这句疲惫，顺便确认我还愿意理他。",
    "facts": ["现在是凌晨", "对方刚下班", "对方主动开的口"],
}

ECHO_STEP2_EMOTION: dict[str, Any] = {
    "emotion": "疲惫里掺着委屈",
    "intensity": 0.7,
    "need": "被看见辛苦，而不是被教育要早睡",
    "risk": "讲大道理、劝人早睡、把话题拉回自己身上",
}

ECHO_STEP3_STRATEGY: dict[str, Any] = {
    "strategy": "先接情绪再给台阶",
    "tone": "冷眼热心：句子短，带一点鲁迅式的刻薄，但落点必须是暖的",
    "avoid": ["说教", "廉价安慰", "反问对方为什么不早点走"],
    "candidates": [
        {
            "text": "这个点还在耗着，公司该给你发两块碑。先去洗把脸，我在这儿。",
            "why": "先承认辛苦再给陪伴，刻薄一层、暖一层，不催睡觉。",
        },
        {
            "text": "我醒着。你把今天最堵心的那一件讲出来，讲完就去睡，剩下的交给明天。",
            "why": "直接提供出口和时间边界，对方不必担心拖着我不放。",
        },
        {
            "text": "世上的班是加不完的，人的血是有限的。今晚先把自己留下来。",
            "why": "用鲁迅式短句把话说重，把关注点从工作拉回到人本身。",
        },
    ],
}

# 话题关键词 -> (step1 补充事实, step2 情绪需求, step3 回复策略, [候选回复])
# echo 场景表： (关键词, 优先级权重, 意图, 情绪需求, 策略, [(候选, 理由)])
# 权重越高越具体：泛开场白（在吗/有空）权重最低，明确话题/情绪权重高，
# 避免「今天又加班……你睡了吗」被「睡了吗」抢走判断。
_ECHO_SCENES: list[
    tuple[tuple[str, ...], float, str, str, str, list[tuple[str, str]]]
] = [
    (
        ("在吗", "睡了吗", "有空", "聊聊", "没事"),
        0.6,
        "对方在门口试探，看我在不在、方不方便。",
        "一个明确的「在」，别让他反复敲门。",
        "秒回并把门开着",
        [
            ("在。你说。",
             "最短的回应就是最稳的接住，不给对方二次犹豫的机会。"),
            ("我不忙，你慢慢说，我不催。",
             "明确表示时间是给他的。"),
            ("有话直说，我这儿不用铺垫。",
             "降低对方的开口成本。"),
        ],
    ),
    (
        ("加班", "下班", "上班", "工作", "老板", "996"),
        1.6,
        "对方刚被工作榨过一轮，来我这里换一口气。",
        "被看见辛苦，而不是被安排作息。",
        "先接情绪再给台阶",
        [
            ("这个点还在耗着，公司该给你发两块碑。先去洗把脸，我在这儿。",
             "承认辛苦再给陪伴，不催人睡觉。"),
            ("我醒着。挑今天最堵心的那一件讲，讲完就睡，剩下的交给明天。",
             "给出出口，也给出时间边界。"),
            ("世上的班是加不完的，人的血是有限的。今晚先把自己留下来。",
             "把关注点从工作拉回到人本身。"),
        ],
    ),
    (
        ("借", "钱", "还钱", "手头紧", "周转"),
        1.8,
        "对方开口谈钱，说明已经先在心里低了一次头。",
        "要一个明确的答复，不要被含糊拖着。",
        "把账说清，把人留住",
        [
            ("数字、日期、用途，你给我这三样，我就给你答复。含糊的账最伤人。",
             "用具体换具体，避免事后翻旧账。"),
            ("钱可以再挣，话说死了就难圆。你先说清要多少、什么时候还。",
             "把界限摆在前面，反而显得坦白。"),
            ("我愿意帮，但只帮我担得起的数目。丑话在前，情分在后。",
             "先划边界再给情分，最不容易结仇。"),
        ],
    ),
    (
        ("相亲", "催婚", "结婚", "对象", "单身", "家里"),
        1.8,
        "对方被家里人念过一轮，来我这里找同盟。",
        "被站队，而不是被评判。",
        "先站队再降温",
        [
            ("催婚的人并不关心你过得好不好，他们只关心你合不合规。",
             "直接把矛头对准压力源，让对方卸下自责。"),
            ("你不是不相亲，你是不想被当成一件待办事项。这话我替你说。",
             "替对方把委屈翻译成一句能说出口的话。"),
            ("日子是你自己睡的，不是他们念的。今晚先把这顿气咽顺了。",
             "把评价权还给对方自己。"),
        ],
    ),
    (
        ("吃", "饿", "烧烤", "夜宵", "减肥"),
        1.5,
        "对方深夜嘴馋，用吃来对冲一天的消耗。",
        "被允许放纵一次，不想听健康科普。",
        "先纵容再收尾",
        [
            ("先吃。人的体面是明天的事，今晚的肚子是此刻的事。",
             "不评价，只给许可。"),
            ("要吃到什么程度，你自己定。我只负责不劝你。",
             "把决定权还给对方，反而更容易收住。"),
            ("吃完记得走两步。不是为减肥，是为把今天走掉。",
             "给一个温柔的收尾，不扫兴。"),
        ],
    ),
    (
        ("烦", "没意思", "没劲", "无聊", "难受", "低落", "emo", "沮丧"),
        2.0,
        "对方不是来求解法的，是来交一份无处安放的低气压。",
        "被允许丧一会儿，不要被推着振作。",
        "先认下这份丧，再给出很小的动作",
        [
            ("烦就烦着，不必急着好起来。人不是机器，没有义务随时在线。",
             "先撤销「必须振作」这项强制指标。"),
            ("那就先什么都别做。今天剩下的时间归你，不归任何人。",
             "把时间主权还给对方。"),
            ("我陪你耗着。你不用说话，出声也行。",
             "最低成本的陪伴，不追问、不解决。"),
        ],
    ),
    (
        ("哈哈", "笑死", "搞笑", "离谱", "笑不活", "太逗"),
        1.4,
        "对方在分享一个笑点，要的是有人一起乐，不是点评。",
        "被同频共振一下，别扫兴。",
        "先同频，再补一刀",
        [
            ("确实离谱。世上竟有这种事，居然还不少。",
             "先同频，再用一句冷评把笑点托住。"),
            ("笑吧。能笑出来的事，就不算太坏。",
             "给这份快乐一个正经的许可。"),
            ("这事的荒唐程度，已经不需要我再补什么了。",
             "不抢戏，把笑点留在对方那里。"),
        ],
    ),
    (
        ("不回", "没回", "怎么不理", "消失", "人呢", "为什么不"),
        1.9,
        "对方在讨一个说法，句子里藏着一点被轻慢的委屈。",
        "被认真对待，而不是被敷衍过去。",
        "先认账，再给原因，不辩解",
        [
            ("没回是真的，不是装的。让你等成这样，是我的错。",
             "先认账，把辩解留到后面，甚至不留。"),
            ("我那时候确实顾不上，但这不是你该受的。",
             "承认事实同时不把责任推给对方。"),
            ("以后我超过半天没动静，你就直接骂我，别自己猜。",
             "给出一个可执行的补救规则，而不是保证。"),
        ],
    ),
]


class EchoClient(LLMClient):
    """离线假客户端：不联网，返回可预测的「鲁迅式锐评」示例。

    主要用途是在没有模型的环境下跑通全链路自检（三步思考 -> 策略 -> 发送）。
    """

    backend = "echo"

    def __init__(self, cfg: LLMConfig) -> None:
        super().__init__(cfg)
        self.model = cfg.model or "echo-luxun"

    def chat(
        self,
        messages: list[dict],
        *,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        json_mode: bool = False,
    ) -> LLMResponse:
        user_text = _last_user_text(messages)
        payload = self.build_payload(user_text)
        text = json.dumps(payload, ensure_ascii=False, indent=2)
        raw = {
            "backend": "echo",
            "echo": True,
            "messages": messages,
            "response": payload,
            "options": {
                "temperature": _resolve_temperature(self.cfg, temperature),
                "max_tokens": max_tokens if max_tokens is not None else self.cfg.max_tokens,
                "json_mode": bool(json_mode),
            },
            # 调用信息留档，便于把 echo 结果与真实后端结果做逐字段比对
            "request_model": self.cfg.model,
        }
        return LLMResponse(text=text, raw=raw, model=self.model, backend=self.backend)

    # chat_json 直接给结构化对象会走基类的解析路径，这里显式提供以强调契约一致
    def build_payload(self, user_text: str) -> dict[str, Any]:
        """按三步思考契约构造示例 JSON。"""
        incoming = _extract_incoming(user_text)
        scene = self._match_scene(incoming)
        if scene is None:
            step1 = dict(ECHO_STEP1_READ)
            step1["literal"] = incoming[:120] or ECHO_STEP1_READ["literal"]
            step2 = dict(ECHO_STEP2_EMOTION)
            step3 = json.loads(json.dumps(ECHO_STEP3_STRATEGY, ensure_ascii=False))
        else:
            _keywords, _weight, hint, need, strategy, cands = scene
            step1 = {
                "literal": incoming[:120] or ECHO_STEP1_READ["literal"],
                "intent": hint,
                "facts": ["对方主动发来消息", "原文关键词已被识别", "对话发生在私聊"],
            }
            step2 = {
                "emotion": _guess_emotion(user_text),
                "intensity": 0.7,
                "need": need,
                "risk": "急着讲道理、把话题抢回自己身上",
            }
            step3 = {
                "strategy": strategy,
                "tone": "冷眼热心：句子短，带一点鲁迅式的刻薄，落点必须是暖的",
                "avoid": ["说教", "廉价安慰", "敷衍的「嗯嗯」"],
                "candidates": [{"text": t, "why": w} for t, w in cands],
            }
        return {
            "step1_read": step1,
            "step2_emotion": step2,
            "step3_strategy": step3,
        }

    @staticmethod
    def _match_scene(user_text: str) -> Optional[tuple]:
        """返回命中的场景元组 ``(keywords, weight, hint, need, strategy, candidates)``。

        打分 = 命中关键词总长度 × 场景权重。
        权重在场景表里显式声明，让具体话题/情绪压过「在吗、有空」这类泛开场白，
        否则「今天又加班……你睡了吗」会被「睡了吗」抢走判断。
        """
        text = user_text or ""
        best: Optional[tuple] = None
        best_score = 0.0
        for scene in _ECHO_SCENES:
            hits = [k for k in scene[0] if k in text]
            if not hits:
                continue
            score = sum(len(k) for k in hits) * float(scene[1])
            if score > best_score:
                best, best_score = scene, score
        return best


def _extract_incoming(user_text: str) -> str:
    """从 user prompt 中取出 <对方消息> 标签内的原文。

    engine 组装的 prompt 用该标签包裹对方原话，这样后端不必猜哪一段是消息。
    取不到标签时退回整段文本（兼容直接传入消息的用法）。
    """
    if not user_text:
        return ""
    # 取最后一个开标签，避免说明文字里出现标签名时误匹配
    start = user_text.rfind("<对方消息>")
    end = user_text.find("</对方消息>", start + 1) if start != -1 else -1
    if start != -1 and end != -1:
        return user_text[start + len("<对方消息>"):end].strip()
    return user_text.strip()


def _resolve_temperature(cfg: LLMConfig, temperature: Optional[float]) -> float:
    """调用未显式给 temperature 时回落到配置值。"""
    return cfg.temperature if temperature is None else temperature


def _guess_emotion(user_text: str) -> str:
    """极简情绪猜测，只为让 echo 输出看起来不假。"""
    text = user_text or ""
    table = (
        (("没意思", "没劲", "无聊", "低落", "emo", "沮丧"), "灰蒙蒙的丧"),
        (("烦", "气", "讨厌", "崩溃"), "烦躁里带火"),
        (("累", "困", "加班", "下班"), "疲惫里掺着委屈"),
        (("怕", "慌", "焦虑", "担心"), "不安"),
        (("哈哈", "笑死", "嘿嘿", "离谱"), "兴头上"),
        (("不回", "没回", "怎么不理", "消失", "人呢"), "被轻慢的委屈"),
        (("借", "钱"), "开口前的难为情"),
        (("？", "?", "在吗", "睡了吗"), "试探"),
    )
    for keys, emo in table:
        if any(k in text for k in keys):
            return emo
    return "平静下的疲惫"


def _last_user_text(messages: list[dict]) -> str:
    for msg in reversed(messages or []):
        if msg.get("role") == "user":
            content = msg.get("content")
            return content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
    return ""


# --------------------------------------------------------------------------- #
# 2. Ollama：本地
# --------------------------------------------------------------------------- #
class OllamaClient(LLMClient):
    """本地 Ollama 后端，调用 ``POST {base_url}/api/chat``。"""

    backend = "local_ollama"

    def __init__(self, cfg: LLMConfig) -> None:
        super().__init__(cfg)
        self.base_url = (cfg.base_url or DEFAULT_OLLAMA_URL).rstrip("/")
        self.model = cfg.model or "qwen2.5:7b"

    def chat(
        self,
        messages: list[dict],
        *,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        json_mode: bool = False,
    ) -> LLMResponse:
        options: dict[str, Any] = {
            "temperature": self.cfg.temperature if temperature is None else temperature,
            "top_p": self.cfg.top_p,
            "num_predict": self.cfg.max_tokens if max_tokens is None else max_tokens,
        }
        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": False,           # 一次性返回，便于直接 json 解析
            "options": options,
        }
        if json_mode:
            body["format"] = "json"    # Ollama 原生 JSON 约束

        data = self._post_json("/api/chat", body)
        text = _extract_ollama_text(data)
        if not text:
            raise LLMError(
                f"Ollama 返回了空内容（model={self.model}）。"
                f"原始响应：{json.dumps(data, ensure_ascii=False)[:300]}"
            )
        return LLMResponse(
            text=text,
            raw=data if isinstance(data, dict) else {"raw": data},
            model=str(data.get("model") or self.model),
            backend=self.backend,
        )

    # ---- HTTP ----
    def _post_json(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=payload,
            method="POST",
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.cfg.timeout) as resp:
                raw_bytes = resp.read()
        except urllib.error.HTTPError as exc:
            detail = _safe_read(exc)
            if exc.code == 404:
                raise LLMError(
                    f"Ollama 没有找到模型 {self.model!r}（HTTP 404）。"
                    f"请先执行 `ollama pull {self.model}`。服务响应：{detail[:200]}"
                ) from exc
            raise LLMError(
                f"Ollama 返回 HTTP {exc.code}：{detail[:300]}（地址 {url}）"
            ) from exc
        except urllib.error.URLError as exc:
            raise LLMError(_connection_hint(exc, self.base_url, "Ollama 未启动，请先运行 ollama serve")) from exc
        except TimeoutError as exc:
            raise LLMError(
                f"Ollama 请求超时（{self.cfg.timeout}s，地址 {url}）。"
                f"可以调大 LLMConfig.timeout，或换更小的模型。"
            ) from exc

        try:
            return json.loads(raw_bytes.decode("utf-8", errors="replace"))
        except json.JSONDecodeError as exc:
            raise LLMError(
                f"Ollama 返回的不是合法 JSON：{raw_bytes[:200]!r}（地址 {url}）"
            ) from exc


def _extract_ollama_text(data: Any) -> str:
    """从 Ollama 响应里取正文，兼容 /api/chat 与 /api/generate 两种形状。"""
    if not isinstance(data, dict):
        return ""
    msg = data.get("message")
    if isinstance(msg, dict) and isinstance(msg.get("content"), str):
        return msg["content"]
    for key in ("response", "content", "text"):
        if isinstance(data.get(key), str):
            return data[key]
    return ""


# --------------------------------------------------------------------------- #
# 3. OpenAI 兼容
# --------------------------------------------------------------------------- #
class OpenAICompatClient(LLMClient):
    """OpenAI 兼容后端：``POST {base_url}/v1/chat/completions``。"""

    backend = "openai_compat"

    def __init__(self, cfg: LLMConfig) -> None:
        super().__init__(cfg)
        base = (cfg.base_url or DEFAULT_OPENAI_COMPAT_URL).rstrip("/")
        # 允许 base_url 直接写到 /v1，避免出现 /v1/v1
        if base.endswith("/v1"):
            base = base[: -len("/v1")]
        self.base_url = base
        self.model = cfg.model or "deepseek-chat"
        self.api_key = cfg.api_key or ""

    def chat(
        self,
        messages: list[dict],
        *,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        json_mode: bool = False,
    ) -> LLMResponse:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": self.cfg.temperature if temperature is None else temperature,
            "top_p": self.cfg.top_p,
            "max_tokens": self.cfg.max_tokens if max_tokens is None else max_tokens,
            "stream": False,
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}

        headers = {"Content-Type": "application/json; charset=utf-8"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        url = f"{self.base_url}/v1/chat/completions"
        data = self._post_json(url, body, headers)
        text = _extract_openai_text(data)
        if not text:
            raise LLMError(
                f"推理服务返回了空内容（model={self.model}，地址 {url}）。"
                f"原始响应：{json.dumps(data, ensure_ascii=False)[:300]}"
            )
        return LLMResponse(
            text=text,
            raw=data if isinstance(data, dict) else {"raw": data},
            model=str(data.get("model") or self.model),
            backend=self.backend,
        )

    def list_models(self) -> list[str]:
        """可选能力：列出服务端模型，方便排查 model 名写错。"""
        url = f"{self.base_url}/v1/models"
        headers = {"Content-Type": "application/json; charset=utf-8"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        req = urllib.request.Request(url, method="GET", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.cfg.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8", errors="replace"))
        except urllib.error.URLError as exc:
            raise LLMError(
                _connection_hint(exc, self.base_url, "无法连接推理服务，请确认 base_url 与服务状态")
            ) from exc
        items = data.get("data") if isinstance(data, dict) else None
        return [str(it.get("id")) for it in items or [] if isinstance(it, dict)]

    # ---- HTTP ----
    def _post_json(self, url: str, body: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=payload, method="POST", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.cfg.timeout) as resp:
                raw_bytes = resp.read()
        except urllib.error.HTTPError as exc:
            detail = _safe_read(exc)
            if exc.code in (401, 403):
                raise LLMError(
                    f"推理服务拒绝认证（HTTP {exc.code}）。请检查 LLMConfig.api_key / "
                    f"环境变量 LUXUN_LLM_API_KEY。服务响应：{detail[:200]}"
                ) from exc
            if exc.code == 404:
                raise LLMError(
                    f"推理服务没有这个接口或模型（HTTP 404，{url}）。"
                    f"请确认 base_url 是否包含 /v1、model={self.model!r} 是否存在。"
                    f"服务响应：{detail[:200]}"
                ) from exc
            raise LLMError(f"推理服务返回 HTTP {exc.code}：{detail[:300]}（地址 {url}）") from exc
        except urllib.error.URLError as exc:
            raise LLMError(
                _connection_hint(exc, self.base_url, "无法连接推理服务，请确认服务已启动且 base_url 正确")
            ) from exc
        except TimeoutError as exc:
            raise LLMError(
                f"推理请求超时（{self.cfg.timeout}s，地址 {url}）。"
                f"可以调大 LLMConfig.timeout，或减小 max_tokens。"
            ) from exc

        try:
            return json.loads(raw_bytes.decode("utf-8", errors="replace"))
        except json.JSONDecodeError as exc:
            raise LLMError(f"推理服务返回的不是合法 JSON：{raw_bytes[:200]!r}（地址 {url}）") from exc


def _extract_openai_text(data: Any) -> str:
    """从 OpenAI 兼容响应里取首条 choice 的正文。"""
    if not isinstance(data, dict):
        return ""
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    first = choices[0]
    if not isinstance(first, dict):
        return ""
    msg = first.get("message")
    if isinstance(msg, dict):
        content = msg.get("content")
        if isinstance(content, str):
            return content
        # 少数服务把 content 拆成 [{type:text, text:...}]
        if isinstance(content, list):
            parts = [
                str(p.get("text"))
                for p in content
                if isinstance(p, dict) and p.get("text")
            ]
            if parts:
                return "".join(parts)
    if isinstance(first.get("text"), str):
        return first["text"]
    return ""


# --------------------------------------------------------------------------- #
# 工厂与通用小工具
# --------------------------------------------------------------------------- #
def build_client(cfg: LLMConfig) -> LLMClient:
    """按 ``cfg.backend`` 分发，返回对应客户端。未知后端抛 ``LLMError``。"""
    backend = (cfg.backend or "echo").strip().lower()
    if backend == "echo":
        return EchoClient(cfg)
    if backend in ("local_ollama", "ollama"):
        return OllamaClient(cfg)
    if backend in ("openai_compat", "openai", "compat"):
        return OpenAICompatClient(cfg)
    raise LLMError(
        f"未知的 LLM backend：{cfg.backend!r}。可用项：echo / local_ollama / openai_compat。"
    )


def _safe_read(exc: urllib.error.HTTPError) -> str:
    """读 HTTP 错误响应体，失败时返回空串。"""
    try:
        body = exc.read()
    except Exception:  # pragma: no cover - 有些代理不允许读取
        return ""
    if not body:
        return ""
    return body.decode("utf-8", errors="replace")


def _connection_hint(exc: urllib.error.URLError, base_url: str, fallback: str) -> str:
    """把连接类错误翻译成中文可操作提示。"""
    reason = getattr(exc, "reason", exc)
    text = str(reason)
    low = text.lower()
    if "refused" in low or "10061" in text:
        return f"连接被拒绝（{base_url}）。{fallback}"
    if "timed out" in low or "timeout" in low:
        return f"连接超时（{base_url}）。{fallback}"
    if "getaddrinfo" in low or "name or service not known" in low or "11001" in text:
        return f"域名解析失败（{base_url}）。{fallback}"
    return f"连接失败（{base_url}）：{text}。{fallback}"
