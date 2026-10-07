# -*- coding: utf-8 -*-
"""语料构建 + 风格检索。

只依赖标准库。三个层次：

1. ``pair_exchanges``  —— 把归一化的 ``Message`` 流切成 ``Exchange``（对方 run → 我的 run）。
2. ``analyze_style``   —— 从「我发的所有消息」里统计出风格画像（字典）。
3. ``build_style_index`` / ``StyleRetriever`` —— 把 Exchange 变成可检索的 few-shot 索引。

设计取舍（重要，会影响效果，见文件末尾的「局限」注释）：

* **中文分词不引入第三方库**。我们退而求其次用「CJK 单字 + 2-gram」当词表。
  这在短文本（QQ 聊天、几条短句）上比真分词差，但对「找相似语气的历史回复」够用，
  而且完全不占依赖。
* **风格统计尽量只统计我发的文本**（``Direction.OUT``），因为检索和生成的 target 都是我。

术语：
    run      连续同方向的消息串
    gap      相邻消息的时间间隔；超过阈值就切断，不跨会话拼接
    noise    纯标点 / 单字重复的短消息（如「。」「哦」「嗯嗯」），默认不参与配对
"""
from __future__ import annotations

import json
import math
import re
import statistics
import time as _time
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

try:  # 正常包内导入
    from .schema import Direction, Exchange, Message, MsgKind
except ImportError:  # pragma: no cover - 允许直接按脚本路径跑（自检脚本会用到）
    from schema import Direction, Exchange, Message, MsgKind  # type: ignore

__all__ = [
    "CJK_STOPWORDS",
    "DEFAULT_STOPWORDS",
    "EMOTION_LEXICON",
    "NOISE_PUNCT",
    "SWEAR_LEXICON",
    "StyleRetriever",
    "analyze_style",
    "build_style_index",
    "build_style_index_from_path",
    "format_examples",
    "load_index",
    "pair_exchanges",
    "read_style_index",
    "tokens_for_match",
    "write_style_index",
    "write_style_report",
]


# --------------------------------------------------------------------------- #
# 0. 词表与常量
# --------------------------------------------------------------------------- #

#: 中文停用词 + 高频虚词 + 常见标点。用于关键词抽取与检索去噪。
#: 说明：这不是学术级停用词表，是按 QQ 聊天语料手感攒的，够用、可读、可改。
CJK_STOPWORDS: frozenset[str] = frozenset(
    """
    的 了 是 在 我 你 他 她 它 我们 你们 他们 咱们 自己 这 那 这个 那个 这些 那些
    这里 那里 这么 那么 这样 那样 什么 为什么 怎么 怎么样 怎样 哪些 哪个 谁 哪里
    吗 呢 吧 啊 呀 哇 哦 噢 喔 嗯 唉 诶 哎 哈 嘿 喂 嘛 咯 喽 嘞 滴 啦 捏 嗷 咦 哼
    么 唷 呦 嗨 呵 咳 唔 嗯嗯 哈哈 呵呵 嘿嘿 嘻嘻
    不 没 没有 别 不要 不用 不能 不是 就是 就 都 也 还 又 再 才 只 只 要 得 地 着 过
    很 太 更 最 挺 好 好像 大概 可能 应该 一定 真的 确实 其实 反正 到底 究竟
    和 与 跟 同 及 或 或者 而 而且 但 但是 不过 因为 所以 由于 如果 要是 虽然 只是
    然后 后来 现在 今天 明天 昨天 最近 以前 以后 刚才 马上 立刻 已经 曾经 一直 总是
    可以 会 能 想 觉得 认为 知道 看到 听说 感觉 有点 一点 一些 一个 一下 一样 一起
    时候 地方 东西 事情 问题 样子 意思 情况 感觉 方法 情况
    请问 请 麻烦 谢谢 感谢 抱歉 对不起 没事 好的 好啊 好呀 行 可以 收到 嗯嗯 在吗
    对 对对 是啊 是的 有 有吗 来了 去吧 走 来
    , 。 , 、 ; : ! ? … ～ ~ . , ; : ! ? " " ' ' ( ) ( ) 【 】 《 》 〈 〉 —— - / \\ | + = * & ^ % $ # @
    """.split()
)

#: 供 ``build_style_index`` 使用的停用词表（中文停用词 + 标点，≥60 条）。
#: 单独导出这个名字是为了让调用方可以「替换/扩展」而不是改我们的常量。
DEFAULT_STOPWORDS: frozenset[str] = CJK_STOPWORDS

#: **单字**虚词集合：用于 2-gram 的单字兜底（只有当**两个字**都是虚词时才丢）。
#: 注意不要把「好」「十」「点」「累」这类能构成实义的字符放进来——
#: 它们单看很虚（"好"），组合起来是关键词（"好累""十点"）。
_CHAR_STOP: frozenset[str] = frozenset(
    "的了是在我你他她它这那有和与就都也还又再才只很太更最挺"
    "吗呢吧啊呀哇哦噢喔嗯唉诶哎哈嘿喂嘛咯喽嘞啦捏嗷咦哼么"
    "不没别让给被把从到对向着过之其而或但以于为将则且"
    "一二三四五六七八九十个"
)

#: 不能出现在 n-gram **开头**的字符（虚词/代词/数词）。
_CHAR_STOP_HEAD: frozenset[str] = frozenset(
    "的了是在我你他她它们这那有一和与就都也还又再才只很太更最挺"
    "吗呢吧啊呀哇哦噢喔嗯唉诶哎哈嘿喂嘛咯喽嘞啦捏嗷咦哼么"
    "不没别让给被把从到对向着过之其而或但以于为将则且"
    "一二三四五六七八九十个"
)

#: 不能出现在 n-gram **结尾**的字符（语助词/语气词/结构助词）。
#: 只放明确的句末语气成分，绝不能放「好」「点」这类实义字。
_CHAR_STOP_TAIL: frozenset[str] = frozenset(
    "的了着过吗呢吧啊呀哇哦噢喔嗯唉诶哎哈嘿喂嘛咯喽嘞啦捏嗷咦哼么"
    "不没很太更最也都还又再才只就而或但以于为将则且"
)

#: 纯标点消息（整段只有这些字符时视为 noise）。
NOISE_PUNCT = "。，、！？…～~,.!?;:;:—-·()（）[]【】“”\"'‘’ 　\t"

#: 笑法词典：pattern -> 归类名。
LAUGH_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"哈{2,}", "哈哈"),
    (r"(?i)\bh{3,}\b|(?:^|[^a-z])h{3,}", "hhh"),
    (r"(?i)\b233+\b|\bhhh?\b(?![a-z])", "233"),
    (r"[笑微笑偷笑捂脸]", "文字笑"),
    (r"[😂🤣😄😆😁😀😃😹]", "emoji笑"),
    (r"[xXｘＸ]{3,}", "xxx笑"),
    (r"嘿嘿|嘻嘻|呵呵|噗|哈哈镜", "其他笑"),
)

#: 情绪启发式词表。**顺序即优先级**：越具体的类别越靠前。
#: 分类词优先于疑问（"今天好累啊" 应判负面而不是疑问）。
EMOTION_LEXICON: dict[str, tuple[str, ...]] = {
    "求助": ("帮我", "救命", "求助", "不会", "咋弄", "怎么办", "如何", "教教", "教我",
             "咋办", "求教", "怎么搞", "报错", "崩了", "挂了", "坏了", "推荐一下"),
    "负面": ("累", "烦", "无语", "难受", "emo", "崩溃", "痛苦", "难过", "想哭", "委屈",
             "生气", "气死", "恶心", "失望", "绝望", "焦虑", "压力", "抑郁", "摆烂",
             "烂", "菜", "翻车", "加班", "熬夜", "失眠", "疼", "痛", "惨", "倒霉"),
    "正面": ("开心", "高兴", "哈哈", "嘻嘻", "爽", "快乐", "幸福", "好耶", "赞", "喜欢",
             "爱了", "太好了", "棒", "牛逼", "厉害", "感动", "惊喜", "美滋滋", "舒服"),
    "试探": ("在吗", "在么", "在不在", "忙着吗", "忙不忙", "睡了", "睡了吗", "方便吗",
             "有空吗", "打扰", "问一下", "冒昧", "顺便问"),
    "疑问": ("吗", "呢", "?", "？", "是不是", "有没有", "能不能", "要不要", "为什么",
             "怎么", "哪里", "哪个", "啥", "多少", "何时"),
    "闲聊": ("吃饭", "天气", "周末", "周日", "周一", "放假", "游戏", "电影", "音乐",
             "睡觉", "上班", "下班", "上课", "考试", "旅游", "咖啡", "奶茶", "无聊"),
}

#: 脏话 / 攻击性词。**只放真正带攻击性的词**——
#: 语助词、"大约""罢了"这类反语标记属于 ``RHETORIC_LEXICON``；
#: "无语""服了"这类轻度吐槽也不在此列，否则统计就没法反映真实的脾气。
SWEAR_LEXICON: tuple[str, ...] = (
    "傻逼", "沙比", "煞笔", "智障", "脑残", "傻", "蠢", "笨", "废物", "垃圾", "狗屎",
    "扯淡", "放屁", "胡扯", "有病", "神经病", "恶心", "讨厌", "闭嘴", "滚",
    "烂活", "拉胯", "菜鸡", "白给",
)

#: 鲁迅式锐评腔的修辞标记词（不是脏话，是腔调）。用于 ``swear_or_sharp.rhetoric``。
RHETORIC_LEXICON: tuple[str, ...] = (
    "罢了", "然而", "的确", "向来", "大约", "未必", "不惮", "恶意", "奴才", "看客",
    "阿Q", "吃人", "伪", "荒唐", "荒谬", "可笑", "滑稽", "离谱", "无语", "服了",
    "醉了", "绝了", "逆天", "抽象", "诡辩", "歪理", "也只好", "罢",
)

#: 常见颜文字 / emoji 兜底串（正则覆盖不到的窄范围符号）。
KAOMOJI_HINTS: tuple[str, ...] = (
    "((", "))", "orz", "OTL", "hhh", "233", "www", "= =", "- -", "qwq", "QAQ", "TAT",
    "嘤", "2333", "23333", "……", "。。。",
)

# Unicode emoji 的近似区间（够覆盖 QQ 常用表情，不求完备）。
_EMOJI_RE = re.compile(
    "["
    "\U0001F300-\U0001FAFF"
    "\U0001F1E6-\U0001F1FF"
    "\U00002600-\U000027BF"
    "\U0001F000-\U0001F2FF"
    "\u2b00-\u2bff"
    "\u2600-\u26ff"
    "\ufe0f\u200d"
    "]+",
    flags=re.UNICODE,
)
# 颜文字：只认「窄符号脸」，避免把「（真的）」这类中文括号当作颜文字。
_KAOMOJI_RE = re.compile(r"[\^＞><=；;°ﾟ▽ω´｀∀]+|[（(][\^＞><=；;°ﾟ▽ω´｀∀*\-_/\\ツシ][^)）\n]{0,10}[)）]")
_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
_TOKEN_RE = re.compile(r"[a-zA-Z]+|[0-9]+")
_REPEAT_RE = re.compile(r"(.)\1{2,}")           # 3 个及以上同字压成 1 个（哈哈哈 -> 哈）
_SPACE_RE = re.compile(r"[ \t\u3000]+")
_CLAUSE_SPLIT_RE = re.compile(r"[，。！？!?；;、~～…\n\r,]+")
_WS_COLLAPSE_RE = re.compile(r"\s+")


# --------------------------------------------------------------------------- #
# 1. 小工具
# --------------------------------------------------------------------------- #

def _text_of(m: Message | Mapping[str, Any]) -> str:
    """兼容 Message 与 dict。"""
    if isinstance(m, Mapping):
        return (m.get("text") or "").strip()
    return (getattr(m, "text", "") or "").strip()


def _direction_of(m: Message | Mapping[str, Any]) -> Direction | None:
    if isinstance(m, Mapping):
        raw = m.get("direction")
    else:
        raw = getattr(m, "direction", None)
    if isinstance(raw, Direction):
        return raw
    if raw is None:
        return None
    try:
        return Direction(str(raw))
    except ValueError:
        return None


def _ts_of(m: Message | Mapping[str, Any]) -> int:
    if isinstance(m, Mapping):
        raw = m.get("ts", 0)
    else:
        raw = getattr(m, "ts", 0)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


def _peer_of(m: Message | Mapping[str, Any]) -> str:
    if isinstance(m, Mapping):
        return str(m.get("peer_id") or "")
    return str(getattr(m, "peer_id", "") or "")


def _kind_of(m: Message | Mapping[str, Any]) -> str:
    if isinstance(m, Mapping):
        raw = m.get("kind")
    else:
        raw = getattr(m, "kind", None)
    if isinstance(raw, MsgKind):
        return raw.value
    return str(raw or "text")


def normalize_text(text: str) -> str:
    """归一化：压掉 3+ 连续重复字符、合并空白。只用于统计与检索。"""
    if not text:
        return ""
    t = text.strip()
    t = _SPACE_RE.sub(" ", t)
    t = _REPEAT_RE.sub(r"\1", t)
    return t.strip()


def is_noise_text(text: str, *, max_chars: int = 2, drops: Sequence[str] | None = None) -> bool:
    """判断文本是否为「太短的噪声」（用于丢弃「嗯。」这种无信息 run）。

    规则（三条，任一命中即算噪声）：

    1. 去掉空白后为空；
    2. 整段由标点/空白构成（如 ``。``、``……``、``？？``、``!!``）；
    3. 「单字填充音」：按**字符种类**看去重后的长度 < ``max_chars``（默认 2），
       即整段只有一种字符（``嗯``、``哦``、``嗯嗯``、``哦哦``、``……``）——
       覆盖需求里说的「只有'嗯'、'哦'、'。'且长度 < 2」那一类。

    注意：像「哈哈」「草」「好的」「谢谢」这种字符种类 ≥ 2 的短消息**不算噪声**，
    它们是真实的短回应。副作用是**单个字**的回复（``好`` / ``行`` / ``草``）
    会一起被丢掉——按字符种类无法把「好」和「嗯」分开，宁可错杀单字。
    想保留就把 ``drop_noise`` 置 False。
    """
    t = (text or "").strip()
    if not t:
        return True
    if drops is not None and t in set(drops):
        return True

    core = t.replace(" ", "").replace("\u3000", "")
    if not core:
        return True

    # 规则 2：纯标点
    if all(ch in NOISE_PUNCT for ch in core):
        return True

    # 规则 3：填充音 —— 只有一种字符，且很短
    letters = {ch for ch in core if ch not in NOISE_PUNCT}
    if len(letters) <= 1 and len(core) <= max(2, int(max_chars)):
        return True
    return False


def split_sentences(text: str) -> list[str]:
    """粗切句：按标点切，保留非空片段。"""
    return [p for p in (s.strip() for s in _CLAUSE_SPLIT_RE.split(text or "")) if p]


def _percentile(sorted_vals: Sequence[int], q: float) -> float:
    """线性插值分位数；输入需已排序。空列表返回 0.0。"""
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return float(sorted_vals[0])
    idx = (len(sorted_vals) - 1) * q
    lo = math.floor(idx)
    hi = math.ceil(idx)
    if lo == hi:
        return float(sorted_vals[int(idx)])
    frac = idx - lo
    return float(sorted_vals[lo] * (1 - frac) + sorted_vals[hi] * frac)


# --------------------------------------------------------------------------- #
# 2. pair_exchanges：Message 流 -> Exchange 列表
# --------------------------------------------------------------------------- #

def pair_exchanges(
    messages: list[Message],
    *,
    peer_id: str,
    max_in_run: int = 6,
    max_out_run: int = 4,
    gap_seconds: int = 1800,
    drop_noise: bool = True,
    noise_max_chars: int = 2,
    keep_raw: bool = True,
) -> list[Exchange]:
    """把消息流切成「对方连续若干条 → 我连续若干条」的 ``Exchange``。

    配对规则（逐条实现，便于对照）：

    1. **过滤会话**：只保留 ``peer_id`` 匹配的消息（``peer_id`` 为空串时表示不过滤）。
    2. **排序**：按 ``ts`` 升序（稳定排序，同秒消息保持输入顺序）。
    3. **切会话**：相邻两条消息 ``ts`` 间隔 > ``gap_seconds`` 时强制断开
       （同一会话内也不会跨天/跨会话拼接）。
    4. **聚 run**：连续同方向的消息聚成一个 run。方向切换即结束当前 run。
    5. **配对**：``Exchange`` = 一个 IN run + 紧随其后的 OUT run。
       * 没有紧跟 OUT run 的 IN run 直接丢弃（对方的话我没有回应）；
       * 连续多个 IN run 不可能出现（同方向会被聚成一个 run）；
       * 会话开头的 OUT run（我没头没脑先说话）不构成 Exchange，丢弃。
    6. **文本过滤**：``text`` 为空的消息（图片/语音/文件等）不参与配对；
       但 ``keep_raw=True`` 时它们仍会作为「有上下文价值」保留——
       具体做法是：run 里夹着的空文本消息被保留在 ``incoming`` / ``reply`` 中，
       而**整段都是空文本**的 run 直接丢弃。
    7. **噪声过滤**（``drop_noise=True``）：整段满足 ``is_noise_text`` 的 run 视为噪声丢弃
       （如对方只发了个「。」）；置 ``False`` 可关闭。
    8. **长度上限**：``max_in_run`` / ``max_out_run`` 截断过长的 run（取靠近配对点的尾部：
       IN 取最后 N 条，OUT 取最前 N 条——都是离「我的回应」最近的部分）。

    Args:
        messages: 归一化后的消息列表（可跨会话、可乱序）。
        peer_id: 目标会话的对方 QQ 号；空串表示全部会话（内部按 peer 分组处理）。
        max_in_run: 一个 Exchange 里保留的对方消息上限。
        max_out_run: 一个 Exchange 里保留的我的消息上限。
        gap_seconds: 会话切断阈值（秒），默认 1800（半小时）。
        drop_noise: 是否丢弃纯噪声 run。
        noise_max_chars: 噪声判定的短文本阈值，见 ``is_noise_text``。
        keep_raw: 是否保留 run 内的空文本消息（图片/文件等）。

    Returns:
        按时间升序的 ``Exchange`` 列表；``ts`` 取该 Exchange 第一条 OUT 消息的时间。
    """
    max_in = max(1, int(max_in_run))
    max_out = max(1, int(max_out_run))
    gap = max(0, int(gap_seconds))

    # 1. 过滤 + 2. 排序
    msgs = [m for m in messages if _direction_of(m) is not None]
    if peer_id:
        msgs = [m for m in msgs if _peer_of(m) == str(peer_id)]
    if not msgs:
        return []
    msgs = sorted(msgs, key=_ts_of)

    out: list[Exchange] = []

    def flush(peer: str, in_run: list[Message], out_run: list[Message]) -> None:
        """尝试把一个 (IN run, OUT run) 落成 Exchange。"""
        if not in_run or not out_run:
            return  # 规则 5：只有成对的才要
        # 规则 7：噪声
        if drop_noise:
            if is_noise_text("".join(_text_of(m) for m in in_run), max_chars=noise_max_chars):
                return
            if is_noise_text("".join(_text_of(m) for m in out_run), max_chars=noise_max_chars):
                return
        # 规则 6：整段空文本 -> 丢；否则（含非文本消息）保留
        has_in_text = any(_text_of(m) for m in in_run)
        has_out_text = any(_text_of(m) for m in out_run)
        if not has_in_text or not has_out_text:
            return
        in_kept = _trim_run(in_run, max_in, from_tail=True, keep_raw=keep_raw)
        out_kept = _trim_run(out_run, max_out, from_tail=False, keep_raw=keep_raw)
        if not any(_text_of(m) for m in out_kept):
            return
        # ts = 我开口的时间（更贴近「我要在什么时刻说话」）
        ts = _ts_of(out_kept[0]) or _ts_of(in_run[-1])
        out.append(Exchange(peer_id=peer, incoming=list(in_kept), reply=list(out_kept), ts=ts))

    # 3~5. 按会话切分 + 聚 run + 配对
    run_dir: Direction | None = None
    run_peer: str = ""
    run: list[Message] = []
    in_run: list[Message] = []
    prev_ts: int | None = None

    def close_run() -> None:
        nonlocal run, run_dir, in_run
        if not run or run_dir is None:
            run, run_dir = [], None
            return
        if run_dir is Direction.IN:
            in_run = run                      # 开启/替换当前 IN run
        else:                                  # OUT run 收尾 -> 尝试配对
            flush(run_peer, in_run, run)
            in_run = []                        # 一个 IN run 只用一次
        run, run_dir = [], None

    for m in msgs:
        d = _direction_of(m)
        if d is None:
            continue
        ts = _ts_of(m)
        peer = _peer_of(m)
        # 会话切断：peer 变了或时间跳跃
        if run_dir is not None and (peer != run_peer or (prev_ts is not None and ts - prev_ts > gap)):
            close_run()
            in_run, prev_ts = [], None
            if peer != run_peer:
                in_run = []
        if run_dir is not None and d is not run_dir:
            close_run()
        run_dir = d
        run_peer = peer
        run.append(m)
        prev_ts = ts
    close_run()

    out.sort(key=lambda e: e.ts)
    return out


def _trim_run(run: list[Message], limit: int, *, from_tail: bool,
              keep_raw: bool) -> list[Message]:
    """截断 run；``keep_raw=False`` 时顺带丢掉空文本消息。"""
    items = list(run)
    if not keep_raw:
        items = [m for m in items if _text_of(m)]
    if len(items) > limit:
        items = items[-limit:] if from_tail else items[:limit]
    return items


def exchanges_to_jsonl(path: str | Path, exchanges: Iterable[Exchange]) -> int:
    """把 Exchange 写成 JSONL，返回条数（顺手的小工具，非必需 API）。"""
    n = 0
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8", newline="\n") as f:
        for ex in exchanges:
            f.write(json.dumps(ex.to_dict(), ensure_ascii=False) + "\n")
            n += 1
    return n


# --------------------------------------------------------------------------- #
# 3. analyze_style：我的聊天风格画像
# --------------------------------------------------------------------------- #

def _my_texts(my_messages: Sequence[Message | Mapping[str, Any]]
              ) -> tuple[list[str], list[Message | Mapping[str, Any]]]:
    """挑出「非空、且方向不是 IN」的消息文本。

    * ``Direction.OUT`` 一定算我的；
    * ``direction=None``（例如裸 dict 没给方向）也按我的处理，方便单测；
    * ``Direction.IN`` 会被剔除。

    **注意**：``message_splitting`` 必须传**含双方消息的混合列表**——
    只传我的消息会丢掉「对方插话」这个切段信号，导致整场对话被当成一轮。
    其他统计项传什么都一样（它们只看 OUT）。
    """
    texts: list[str] = []
    kept: list[Message | Mapping[str, Any]] = []
    for m in my_messages:
        d = _direction_of(m)
        if d is not None and d is not Direction.OUT:
            continue  # 显式标了 IN 的就不要
        t = _text_of(m)
        if not t:
            continue
        texts.append(t)
        kept.append(m)
    return texts, kept


def extract_emoji(text: str) -> list[str]:
    """抽出 emoji 与颜文字片段（按出现顺序，已去重前的原始计数）。"""
    found: list[str] = []
    t = text or ""
    for mo in _EMOJI_RE.finditer(t):
        s = mo.group(0).replace("\ufe0f", "").replace("\u200d", "")
        if s:
            found.append(s)
    # 常见颜文字兜底（正则覆盖不到的固定串）
    for hint in KAOMOJI_HINTS:
        if hint in ("((", "))", "……", "。"):
            continue
        if hint in t:
            found.append(hint)
    for mo in _KAOMOJI_RE.finditer(t):
        s = mo.group(0).strip()
        if not s or s in ("((", "))", "（）"):
            continue
        # 纯 CJK 的括号内容（"（真的）"）不是颜文字
        if _CJK_RE.search(s) and not re.search(r"[\^＞><=；;°ﾟ▽ω´｀∀]", s):
            continue
        found.append(s)
    return found


def _ngram_counter(texts: Sequence[str], n_min: int, n_max: int, *,
                   per_message_cap: int = 2) -> Counter[str]:
    """统计 n-gram 频次。

    * 先去掉空白（中文里的空格往往是装饰，不应切断 n-gram）；
    * 再按标点切成小句，避免跨句拼出「假短语」；
    * 每个 n-gram 在**单条消息**里最多计 ``per_message_cap`` 次，
      防止一个人连刷 10 遍「哈哈哈哈」把榜单刷爆。
    """
    counter: Counter[str] = Counter()
    for raw in texts:
        t = normalize_text(raw).replace(" ", "")
        if not t:
            continue
        units: list[str] = []
        for clause in _CLAUSE_SPLIT_RE.split(t):
            clause = clause.strip()
            if clause:
                units.append(clause)
        if not units:
            units = [t]
        local: Counter[str] = Counter()
        max_n = min(n_max, max((len(u) for u in units), default=0))
        for n in range(n_min, max_n + 1):
            for u in units:
                if len(u) < n:
                    continue
                for i in range(len(u) - n + 1):
                    g = u[i:i + n]
                    if g.strip():
                        local[g] += 1
        for g, c in local.items():
            counter[g] += min(c, per_message_cap)
    return counter


def _ngram_bad_edges(g: str) -> bool:
    """n-gram 首尾是否为虚词/语助词（是则不宜当短语）。"""
    if not g:
        return True
    if g[0] in _CHAR_STOP_HEAD or g[-1] in _CHAR_STOP_TAIL:
        return True
    if len(g) == 2 and g[0] in _CHAR_STOP and g[1] in _CHAR_STOP:
        return True
    return False


def _is_meaningful_ngram(g: str, stopwords: frozenset[str]) -> bool:
    """过滤明显无意义的 n-gram（全停用词、含标点、首尾是虚词、无中文无字母）。

    首尾虚词过滤用模块级的 ``_CHAR_STOP_HEAD`` / ``_CHAR_STOP_TAIL``，
    它们是**按字符位置**设计的：只拦首尾，不拦中间（"好累""该不该"要留着）。
    """
    if not g or len(g) < 2:
        return False
    if g in stopwords:
        return False
    if re.search(r"[^\w\u3400-\u4dbf\u4e00-\u9fff]", g):
        return False
    if all(ch in stopwords for ch in g):
        return False
    if not (_CJK_RE.search(g) or re.search(r"[a-zA-Z0-9]", g)):
        return False
    # 掐掉「的X」「X了」这类模板化的东西
    if g.startswith(("的", "了", "是", "在", "我", "你")) or g.endswith(("的", "了")):
        return False
    if _ngram_bad_edges(g):
        return False
    return True


def is_filler_text(text: str) -> bool:
    """是否是无信息量的语气填充（不参与 opener/closer/catchphrase 统计）。

    判定：去空白后为空 / 全同字符（"嗯嗯""哈哈哈""。"）/ 全标点。
    注意「谢谢」「好的」这类**字符不同**的短词不算填充，它们是有意义的习惯用语。
    """
    t = (text or "").strip().replace(" ", "").replace("\u3000", "")
    if not t:
        return True
    if len(set(t)) == 1:
        return True
    if all(ch in NOISE_PUNCT for ch in t):
        return True
    return False


def _ngram_rank(counter: Counter[str], stopwords: frozenset[str], *,
                limit: int, n_min: int = 2, n_max: int = 5) -> list[dict[str, Any]]:
    """n-gram 打分排序：频次为主，长度加权，并做「子串去重」。"""
    cands: list[tuple[str, int]] = [
        (g, c) for g, c in counter.items()
        if n_min <= len(g) <= n_max and c >= 2
        and _is_meaningful_ngram(g, stopwords)
    ]
    if not cands:
        # 语料太少时放宽到「出现 1 次」，否则报告会空
        cands = [(g, c) for g, c in counter.items()
                 if n_min <= len(g) <= n_max
                 and _is_meaningful_ngram(g, stopwords)]
    # 打分：频次 * (1 + 0.35 * 额外长度)，长一点的短语信息量更大
    def score(item: tuple[str, int]) -> float:
        g, c = item
        return c * (1.0 + 0.35 * (len(g) - n_min))

    cands.sort(key=lambda x: (-score(x), -len(x[0]), x[0]))
    picked: list[dict[str, Any]] = []
    for g, c in cands:
        # 子串去重：已经被选中的更长短语包含它，且频次差别不大 -> 跳过
        if any(g in p["phrase"] and c <= p["count"] for p in picked):
            continue
        picked.append({"phrase": g, "count": c, "score": round(score((g, c)), 3)})
        if len(picked) >= limit:
            break
    return picked


def _opener_closer(texts: Sequence[str], *, limit: int = 20,
                   stopwords: frozenset[str] = CJK_STOPWORDS
                   ) -> tuple[list[dict], list[dict]]:
    """开头词 / 结尾词。用「开头 2-gram」「结尾 2-gram」近似。"""
    heads: Counter[str] = Counter()
    tails: Counter[str] = Counter()
    for raw in texts:
        if is_filler_text(raw):
            continue                      # 「嗯嗯」「。」不配当口癖
        t = normalize_text(raw).replace(" ", "")
        if len(t) < 2:
            continue
        heads[t[:2]] += 1
        tails[t[-2:]] += 1
    for c in (heads, tails):
        for k in list(c):
            if not _is_meaningful_ngram(k, stopwords):
                del c[k]

    def top(c: Counter[str]) -> list[dict[str, Any]]:
        return [{"word": w, "count": n} for w, n in c.most_common(limit)]

    return top(heads), top(tails)


def _punctuation_stats(texts: Sequence[str]) -> dict[str, Any]:
    """标点频率。``per_message`` = 每 100 条消息出现多少次，便于横向比较。"""
    groups: dict[str, str] = {
        "逗号": "，,",
        "句号": "。.",
        "问号": "？?",
        "感叹号": "！!",
        "省略号": "…⋯",
        "波浪号": "～~",
        "顿号": "、",
        "冒号": "：:",
        "分号": "；;",
        "引号": "「」『』“”\"'‘’",
        "括号": "（）()【】[]",
    }
    total_msgs = max(1, len(texts))
    out: dict[str, Any] = {}
    for name, chars in groups.items():
        cnt = sum(t.count(ch) for t in texts for ch in chars)
        # 省略号常写作 "。。。" 或 "......"
        if name == "省略号":
            cnt = sum(len(re.findall(r"…+|\.{3,}|。{2,}", t)) for t in texts)
        out[name] = {
            "count": cnt,
            "per_100_msgs": round(cnt / total_msgs * 100, 2),
            "messages_with": sum(1 for t in texts if any(ch in t for ch in chars)),
        }
    return out


def _laugh_stats(texts: Sequence[str]) -> dict[str, Any]:
    """笑法计数。非重叠匹配：一个 '2333' 只算进一个类别。"""
    cnt: Counter[str] = Counter()
    hits = 0
    for t in texts:
        consumed: list[tuple[int, int]] = []
        hit = False
        for pattern, name in LAUGH_PATTERNS:
            for mo in re.finditer(pattern, t):
                if any(not (mo.end() <= a or mo.start() >= b) for a, b in consumed):
                    continue                      # 与已归类片段重叠 -> 跳过
                consumed.append((mo.start(), mo.end()))
                cnt[name] += 1
                hit = True
        if hit:
            hits += 1
    total = max(1, len(texts))
    return {
        "kinds": {k: v for k, v in cnt.most_common()},
        "total_hits": sum(cnt.values()),
        "messages_with_laugh": hits,
        "ratio": round(hits / total, 4),
    }


def _swear_stats(texts: Sequence[str]) -> dict[str, Any]:
    hits: Counter[str] = Counter()
    examples: list[str] = []
    for t in texts:
        matched = False
        for w in SWEAR_LEXICON:
            c = t.count(w)
            if c:
                hits[w] += c
                matched = True
        if matched and len(examples) < 10:
            examples.append(t[:40])
    total_chars = max(1, sum(len(t) for t in texts))
    return {
        "total_hits": sum(hits.values()),
        "distinct": len(hits),
        "per_1000_chars": round(sum(hits.values()) / total_chars * 1000, 2),
        "top": [{"word": w, "count": c} for w, c in hits.most_common(20)],
        "examples": examples,
    }


def _rhetoric_stats(texts: Sequence[str]) -> dict[str, Any]:
    """鲁迅式修辞标记词统计（腔调，不是脏话）。"""
    hits: Counter[str] = Counter()
    examples: list[str] = []
    for t in texts:
        matched = False
        for w in RHETORIC_LEXICON:
            c = t.count(w)
            if c:
                hits[w] += c
                matched = True
        if matched and len(examples) < 10:
            examples.append(t[:40])
    total = max(1, len(texts))
    msgs_with = sum(1 for t in texts if any(w in t for w in RHETORIC_LEXICON))
    return {
        "total_hits": sum(hits.values()),
        "distinct": len(hits),
        "messages_with": msgs_with,
        "ratio": round(msgs_with / total, 4),
        "top": [{"word": w, "count": c} for w, c in hits.most_common(20)],
        "examples": examples,
    }


def _splitting_stats(my_messages: Sequence[Message | Mapping[str, Any]],
                     texts: Sequence[str]) -> dict[str, Any]:
    """碎句风格：我一轮平均发几条、每条平均多少字。

    「一轮」= 我连续发出的一段消息（用时间间隔 > 1800s 或对方插话切断）。
    """
    runs: list[list[str]] = []
    cur: list[str] = []
    prev_ts: int | None = None
    prev_dir: Direction | None = None
    for m in my_messages:
        d = _direction_of(m)
        if d is not None and d is not Direction.OUT:
            # 对方插话 = 我这一段结束
            if cur:
                runs.append(cur)
                cur = []
            prev_ts = None
            prev_dir = d
            continue
        t = _text_of(m)
        ts = _ts_of(m)
        if not t:
            continue
        # 从「对方说话」切回「我说话」也算新的一段（不给 prev_ts 归零是为了继续按间隔切）
        if prev_dir is not None and prev_dir is not Direction.OUT and cur:
            runs.append(cur)
            cur = []
        if prev_ts is not None and ts - prev_ts > 1800:
            if cur:
                runs.append(cur)
            cur = []
        cur.append(t)
        prev_ts = ts
        prev_dir = d if d is not None else Direction.OUT

    sizes = [len(r) for r in runs]
    lens = [len(t) for t in texts]
    multi = sum(1 for s in sizes if s >= 2)
    return {
        "runs": len(runs),
        "avg_msgs_per_run": round(statistics.fmean(sizes), 3) if sizes else 0.0,
        "median_msgs_per_run": _percentile(sorted(sizes), 0.5),
        "max_msgs_per_run": max(sizes) if sizes else 0,
        "multi_msg_run_ratio": round(multi / max(1, len(sizes)), 4),
        "avg_chars_per_msg": round(statistics.fmean(lens), 3) if lens else 0.0,
        "short_msg_ratio": round(
            sum(1 for x in lens if x <= 6) / max(1, len(lens)), 4),
    }


def _active_hours(my_messages: Sequence[Message | Mapping[str, Any]]
                  ) -> dict[str, Any]:
    """24 小时分布（本地时区，用 ``time.localtime``）。

    只统计**我发的文本消息**（``IN`` 与空文本会被剔除）——
    否则「我最活跃的时段」会变成「我们俩最活跃的时段」。
    """
    dist = [0] * 24
    for m in my_messages:
        d = _direction_of(m)
        if d is not None and d is not Direction.OUT:
            continue
        if not _text_of(m):
            continue
        ts = _ts_of(m)
        if ts <= 0:
            continue
        hour = _time.localtime(ts).tm_hour
        dist[hour] += 1
    total = max(1, sum(dist))
    peak = max(range(24), key=lambda h: dist[h]) if sum(dist) else None
    # 4 小时滑窗找「黄金时段」
    best_h, best_sum = None, -1
    for h in range(24):
        s = sum(dist[(h + k) % 24] for k in range(4))
        if s > best_sum:
            best_h, best_sum = h, s
    return {
        "hours": dist,
        "peak_hour": peak,
        "peak_count": dist[peak] if peak is not None else 0,
        "golden_window": [best_h, (best_h + 4) % 24] if best_h is not None else None,
        "share": {str(h): round(c / total, 4) for h, c in enumerate(dist) if c},
    }


def analyze_style(my_messages: list[Message], *, top_ngram: int = 30,
                  top_misc: int = 20) -> dict:
    """产出「我的聊天风格画像」。

    所有统计只取**非空、方向不是 IN** 的消息；文本消息的长度/标点/emoji 用原文，
    n-gram 用归一化文本（避免「哈哈哈哈」把榜单刷爆）。

    Args:
        my_messages: 消息列表。除 ``message_splitting`` 外的统计项传「只有我的消息」
            也可以；但为了让「我一轮发几条」算得准，**建议直接传含双方消息的完整流**，
            因为切段需要「对方插话」这个信号。
        top_ngram: ``catchphrases`` 返回条数（默认 30）。
        top_misc: emoji / opener / closer 的 Top N（默认 20）。

    Returns:
        可直接 JSON 序列化的字典，键：``count`` / ``avg_chars`` / ``median_chars`` /
        ``p90_chars`` / ``max_chars`` / ``punctuation`` / ``emoji_usage`` /
        ``catchphrases`` / ``opener`` / ``closer`` / ``laugh_style`` /
        ``swear_or_sharp``（内含 ``rhetoric``）/ ``message_splitting`` / ``active_hours``。
        **不写盘**——写 Markdown 报告请用 ``write_style_report``。
    """
    texts, kept = _my_texts(my_messages)
    lens = [len(t) for t in texts]
    lens_sorted = sorted(lens)
    total_msgs = len(texts)
    total_chars = sum(lens)

    # 长度分布
    lengths = {
        "count": total_msgs,
        "total_chars": total_chars,
        "avg_chars": round(statistics.fmean(lens), 3) if lens else 0.0,
        "median_chars": _percentile(lens_sorted, 0.5),
        "p90_chars": _percentile(lens_sorted, 0.9),
        "max_chars": max(lens) if lens else 0,
        "min_chars": min(lens) if lens else 0,
        "stdev_chars": round(statistics.pstdev(lens), 3) if len(lens) > 1 else 0.0,
    }

    # emoji / 颜文字
    emo: Counter[str] = Counter()
    for t in texts:
        emo.update(extract_emoji(t))
    emoji_total = sum(emo.values())

    # 口头禅（用归一化文本避免「哈哈哈哈」刷榜；长度/标点/emoji 用原文）
    ngram = _ngram_counter(texts, 2, 5)
    use_stop = frozenset(CJK_STOPWORDS)
    catchphrases = _ngram_rank(ngram, use_stop, limit=top_ngram, n_min=2, n_max=5)

    opener, closer = _opener_closer(texts, limit=top_misc, stopwords=use_stop)

    punct = _punctuation_stats(texts)
    laugh = _laugh_stats(texts)
    swear = _swear_stats(texts)
    swear["rhetoric"] = _rhetoric_stats(texts)
    splitting = _splitting_stats(my_messages, texts)
    hours = _active_hours(my_messages)

    # 句内句读：一句话里平均几个标点 -> 反映「断句密度」
    punct_total = sum(v["count"] for k, v in punct.items() if k != "引号" and k != "括号")
    return {
        "count": total_msgs,
        "total_chars": total_chars,
        "avg_chars": lengths["avg_chars"],
        "median_chars": lengths["median_chars"],
        "p90_chars": lengths["p90_chars"],
        "max_chars": lengths["max_chars"],
        "lengths": lengths,
        "punctuation": punct,
        "punctuation_per_msg": round(punct_total / max(1, total_msgs), 3),
        "emoji_usage": {
            "total": emoji_total,
            "distinct": len(emo),
            "per_100_msgs": round(emoji_total / max(1, total_msgs) * 100, 2),
            "top": [{"emoji": e, "count": c} for e, c in emo.most_common(top_misc)],
        },
        "catchphrases": catchphrases,
        "opener": opener,
        "closer": closer,
        "laugh_style": laugh,
        "swear_or_sharp": swear,
        "message_splitting": splitting,
        "active_hours": hours,
    }


# --------------------------------------------------------------------------- #
# 4. build_style_index：Exchange -> 检索记录
# --------------------------------------------------------------------------- #

def _incoming_text(ex: Exchange | Mapping[str, Any]) -> tuple[str, str]:
    """返回 (合并后的 incoming 文本, 合并后的 reply 文本)。"""
    if isinstance(ex, Mapping):
        inc = ex.get("incoming") or []
        rep = ex.get("reply") or []
    else:
        inc = getattr(ex, "incoming", []) or []
        rep = getattr(ex, "reply", []) or []
    in_text = " ".join(t for t in (_text_of(m) for m in inc) if t)
    rep_parts = [t for t in (_text_of(m) for m in rep) if t]
    return in_text, "\n".join(rep_parts)


def _ex_ts(ex: Exchange | Mapping[str, Any]) -> int:
    if isinstance(ex, Mapping):
        return int(ex.get("ts") or 0)
    return int(getattr(ex, "ts", 0) or 0)


def _ex_peer(ex: Exchange | Mapping[str, Any]) -> str:
    if isinstance(ex, Mapping):
        return str(ex.get("peer_id") or "")
    return str(getattr(ex, "peer_id", "") or "")


def tokenize(text: str, *, min_ascii_len: int = 2) -> list[str]:
    """检索用分词：中文 CJK 单字 + 2-gram，英文/数字按词。

    单字也保留，是为了让「累」「烦」这类单字情绪词在短查询里也能命中；
    2-gram 提供更精确的搭配信号。两者一起进 TF-IDF 空间。
    """
    if not text:
        return []
    t = normalize_text(text).lower()
    tokens: list[str] = []
    buf: list[str] = []

    def flush_cjk() -> None:
        if not buf:
            return
        seq = "".join(buf)
        tokens.extend(seq)                              # 单字
        tokens.extend(seq[i:i + 2] for i in range(len(seq) - 1))  # 2-gram
        buf.clear()

    i = 0
    n = len(t)
    while i < n:
        ch = t[i]
        if _CJK_RE.match(ch):
            buf.append(ch)
            i += 1
            continue
        flush_cjk()
        mo = _TOKEN_RE.match(t, i)
        if mo:
            w = mo.group(0)
            if len(w) >= min_ascii_len or w.isdigit():
                tokens.append(w)
            i = mo.end()
            continue
        i += 1
    flush_cjk()
    return tokens


def tokens_for_match(*parts: Any) -> list[str]:
    """把任意文本/列表拼起来分词，供检索器复用。"""
    chunks: list[str] = []
    for p in parts:
        if p is None:
            continue
        if isinstance(p, (list, tuple, set)):
            chunks.extend(str(x) for x in p if x)
        else:
            chunks.append(str(p))
    return tokenize(" ".join(chunks))


def extract_keywords(incoming_text: str, *, top_k: int = 8,
                     stopwords: Iterable[str] = DEFAULT_STOPWORDS) -> list[str]:
    """从 incoming 里抽关键词。

    做法：中文 2-gram + 英文词，按 ``频次 * (1 + 0.5 * (长度 - 2))`` 加权，
    去掉停用词，并对互为子串的候选去重（保留更长/更高分者）。
    """
    stop = set(stopwords)
    text = normalize_text(incoming_text)
    if not text:
        return []
    grams: Counter[str] = Counter()
    # 英文/数字词
    for w in _TOKEN_RE.findall(text.lower()):
        if len(w) >= 2 and w not in stop:
            grams[w] += 2                       # 英文词信息量高于单字，给点权重
    # 中文 2-gram（按标点切小句，避免跨句假搭配）
    for clause in _CLAUSE_SPLIT_RE.split(text.replace(" ", "")):
        clause = clause.strip()
        for i in range(len(clause) - 1):
            g = clause[i:i + 2]
            if not all(_CJK_RE.match(c) for c in g):
                continue
            if g in stop:
                continue
            # 首尾是虚词/语助词 -> 碎渣（「吗今」「累啊」「的时」）
            if _ngram_bad_edges(g):
                continue
            grams[g] += 1
    if not grams:
        # 退路 1：单字（"你好吗" 这种全是虚词组合的短句，2-gram 会被全滤掉）
        for ch in text.replace(" ", ""):
            if _CJK_RE.match(ch) and ch not in stop:
                grams[ch] += 1
    if not grams:
        # 退路 2：原文里的英文/数字词
        for w in _TOKEN_RE.findall(text.lower()):
            if w:
                grams[w] += 1

    def score(item: tuple[str, int]) -> float:
        g, c = item
        return c * (1.0 + 0.5 * max(0, len(g) - 2))

    ranked = sorted(grams.items(), key=lambda kv: (-score(kv), -len(kv[0]), kv[0]))
    picked: list[str] = []
    for g, c in ranked:
        if any((g in p) for p in picked):
            continue
        picked.append(g)
        if len(picked) >= max(1, top_k):
            break
    return picked


def detect_emotion(text: str) -> str:
    """情绪启发式标签：求助 / 疑问 / 负面 / 正面 / 试探 / 闲聊 / 中性。

    顺序即优先级（越具体越靠前）。纯词表匹配，不保证准确。
    """
    t = (text or "").lower()
    if not t.strip():
        return "中性"
    for label, words in EMOTION_LEXICON.items():
        if any(w in t for w in words):
            return label
    if re.search(r"[?？]{2,}", t):
        return "疑问"
    if re.search(r"[!！]{2,}", t):
        return "正面" if re.search(r"哈哈|好耶|太棒", t) else "闲聊"
    return "中性"


def _truncate(text: str, limit: int) -> str:
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    return text[:limit]


def build_style_index(exchanges: Iterable[Exchange | Mapping[str, Any]], *,
                      max_reply_chars: int = 120) -> list[dict]:
    """为每个 ``Exchange`` 产出一条可检索记录。

    Returns:
        ``[{"id","peer_id","ts","incoming_text","reply_text","reply_len",
        "keywords","emotion_hint"}, ...]``

    * ``incoming_text``：对方那几条拼起来（换行分隔，便于模型理解多气泡）。
    * ``reply_text``：我的那几条拼起来（``\\n`` 分隔），超过 ``max_reply_chars`` 截断；
      但 ``reply_len`` 记录**截断前**的真实长度。
    * ``keywords`` / ``emotion_hint``：见 ``extract_keywords`` / ``detect_emotion``。
    """
    limit = max(1, int(max_reply_chars))
    records: list[dict] = []
    for idx, ex in enumerate(exchanges):
        in_text, rep_text = _incoming_text(ex)
        if not rep_text:
            continue                    # 我没有文本回应 -> 对风格学习无意义
        full_len = len(rep_text)
        rep_short = _truncate(rep_text.replace("\n", " ").strip(), limit)
        records.append({
            "id": f"ex{idx:06d}",
            "peer_id": _ex_peer(ex),
            "ts": _ex_ts(ex),
            "incoming_text": in_text,
            "reply_text": rep_short,
            "reply_len": full_len,
            "keywords": extract_keywords(in_text, top_k=8),
            "emotion_hint": detect_emotion(in_text),
        })
    return records


def write_style_index(path: str | Path, records: Iterable[dict]) -> int:
    """把索引写成 JSONL，返回条数。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with p.open("w", encoding="utf-8", newline="\n") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
            n += 1
    return n


def read_style_index(path: str | Path) -> list[dict]:
    """读 JSONL 索引（兼容 JSON 数组文件）。"""
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    stripped = text.lstrip()
    if stripped.startswith("["):
        data = json.loads(stripped)
        return [d for d in data if isinstance(d, dict)]
    out: list[dict] = []
    for line in text.splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


#: ``StyleRetriever.load`` 的别名，方便调用。
load_index = read_style_index
build_style_index_from_path = read_style_index


# --------------------------------------------------------------------------- #
# 5. StyleRetriever：2-gram + 英文词 的 TF-IDF / 余弦检索
# --------------------------------------------------------------------------- #

class StyleRetriever:
    """轻量风格检索器：TF-IDF + 余弦相似度。

    流程：

    1. 建索引时对每条记录的 ``incoming_text``（可选拼 ``reply_text``）分词，
       统计 DF，算平滑 IDF：``idf(t) = ln((N + 1) / (df(t) + 1)) + 1``；
    2. 查询时用**同一套 idf** 把 query 与 doc 都转成 L2 归一化向量；
    3. 余弦相似度 = 归一化向量点积；低于 ``min_score`` 的丢弃。

    健壮性：空索引 / 空查询 / 未知词 / 非字符串字段都不会抛异常，
    只会返回空结果或 0 分。

    用法::

        idx = build_style_index(pair_exchanges(msgs, peer_id="123"))
        write_style_index("data/corpus/style_index.jsonl", idx)
        r = StyleRetriever.load("data/corpus/style_index.jsonl")
        r.search("今天好累啊", top_k=3)
    """

    def __init__(self, index: list[dict], *, include_reply: bool = True) -> None:
        self.records: list[dict] = [r for r in (index or []) if isinstance(r, dict)]
        self.include_reply = bool(include_reply)
        self._df: Counter[str] = Counter()
        self._idf: dict[str, float] = {}
        self._doc_vecs: list[dict[str, float]] = []
        self._doc_norms: list[float] = []
        self._build()

    # ---- 构建 ----
    def _doc_tokens(self, rec: Mapping[str, Any]) -> list[str]:
        parts: list[str] = [str(rec.get("incoming_text") or "")]
        if self.include_reply:
            parts.append(str(rec.get("reply_text") or ""))
        kw = rec.get("keywords") or []
        if isinstance(kw, (list, tuple)):
            parts.extend(str(k) for k in kw)
        emo = rec.get("emotion_hint")
        if emo:
            parts.append(str(emo))
        return tokens_for_match(*parts)

    def _build(self) -> None:
        n = len(self.records)
        toks_per_doc: list[Counter[str]] = []
        for rec in self.records:
            tf = Counter(self._doc_tokens(rec))
            toks_per_doc.append(tf)
            self._df.update(tf.keys())
        for t, df in self._df.items():
            self._idf[t] = math.log((n + 1) / (df + 1)) + 1.0
        for tf in toks_per_doc:
            vec = self._vectorize_tf(tf)
            self._doc_vecs.append(vec)
            self._doc_norms.append(math.sqrt(sum(v * v for v in vec.values())))

    def _vectorize_tf(self, tf: Mapping[str, float]) -> dict[str, float]:
        if not tf:
            return {}
        max_tf = max(tf.values()) or 1.0
        vec: dict[str, float] = {}
        for t, c in tf.items():
            idf = self._idf.get(t)
            if idf is None:
                # 查询里出现、索引里没有的词：给最小 idf，避免它主导结果
                idf = 1.0
            vec[t] = (0.5 + 0.5 * c / max_tf) * idf        # 亚线性 TF 抑制长文本
        return vec

    @staticmethod
    def _l2(vec: Mapping[str, float]) -> float:
        return math.sqrt(sum(v * v for v in vec.values()))

    # ---- 对外 API ----
    @classmethod
    def load(cls, path: str | Path, *, include_reply: bool = True) -> "StyleRetriever":
        """从 JSONL（或 JSON 数组）文件载入索引。文件不存在时返回空检索器。"""
        p = Path(path)
        if not p.exists():
            return cls([], include_reply=include_reply)
        try:
            records = read_style_index(p)
        except (json.JSONDecodeError, OSError):
            records = []
        return cls(records, include_reply=include_reply)

    def __len__(self) -> int:
        return len(self.records)

    def search(self, query: str, top_k: int = 6, min_score: float = 0.05) -> list[dict]:
        """检索最相似的 ``top_k`` 条历史 Exchange。

        Args:
            query: 当前对方发来的话（可多条拼接）。
            top_k: 返回条数上限。
            min_score: 余弦相似度下限（默认 0.05）。

        Returns:
            记录副本（含 ``score``），按 ``score`` 降序。空索引/空查询返回 ``[]``。
        """
        k = max(0, int(top_k))
        if k == 0 or not self.records or not isinstance(query, str) or not query.strip():
            return []
        q_tf = Counter(tokens_for_match(query))
        if not q_tf:
            return []
        q_vec = self._vectorize_tf(q_tf)
        # 查询词全部不在索引里 -> 无可比信号
        known = {t: v for t, v in q_vec.items() if t in self._idf}
        if not known:
            return []
        q_norm = self._l2(q_vec)
        if q_norm <= 0:
            return []

        scored: list[tuple[float, int]] = []
        for i, vec in enumerate(self._doc_vecs):
            if not vec or self._doc_norms[i] <= 0:
                continue
            # 只在共现词上点积
            small, big = (known, vec) if len(known) <= len(vec) else (vec, known)
            dot = 0.0
            for t, v in small.items():
                w = big.get(t)
                if w:
                    dot += v * w
            if dot <= 0:
                continue
            score = dot / (q_norm * self._doc_norms[i])
            if score >= float(min_score):
                scored.append((score, i))

        scored.sort(key=lambda x: (-x[0], self.records[x[1]].get("ts") or 0))
        out: list[dict] = []
        for score, i in scored[:k]:
            rec = dict(self.records[i])
            rec["score"] = round(float(score), 6)
            out.append(rec)
        return out

    # 便利方法：把关键词也当成查询信号
    def search_with_hint(self, query: str, *, emotion: str = "", top_k: int = 6,
                         min_score: float = 0.05) -> list[dict]:
        """在 query 上拼一层情绪提示再检索（可选的小幅召回增强）。"""
        q = query if not emotion else f"{query} {emotion} {emotion}"
        return self.search(q, top_k=top_k, min_score=min_score)


# --------------------------------------------------------------------------- #
# 6. StyleRetriever -> few-shot 文本
# --------------------------------------------------------------------------- #

def format_examples(results: Sequence[Mapping[str, Any]], *, max_items: int = 0,
                    with_meta: bool = True) -> str:
    """把检索结果拼成「对方：… / 我：…」的中文 few-shot 文本。

    Args:
        results: ``search`` 的返回值。
        max_items: 最多用几条（0 表示全用）。
        with_meta: 是否附上相似度/情绪标签（给模型看时建议关掉，保持干净）。
    """
    items = list(results or [])
    if max_items and max_items > 0:
        items = items[:max_items]
    if not items:
        return "（暂无可用风格示例）"
    blocks: list[str] = []
    for i, r in enumerate(items, 1):
        inc = str(r.get("incoming_text") or "").strip().replace("\n", " / ")
        rep = str(r.get("reply_text") or "").strip().replace("\n", " / ")
        head = f"【示例{i}】"
        if with_meta:
            tag = r.get("emotion_hint") or "中性"
            try:
                sc = float(r.get("score") or 0.0)
            except (TypeError, ValueError):
                sc = 0.0
            head += f"（相似度 {sc:.3f}｜情绪 {tag}）"
        blocks.append(f"{head}\n对方：{inc}\n我：{rep}")
    return "\n\n".join(blocks)


def to_style_guide(retriever: "StyleRetriever", query: str = "", *,
                   top_k: int = 6, min_score: float = 0.05,
                   max_reply_chars: int = 120) -> str:
    """检索 + 拼装的一条龙：``search`` 之后渲染成 few-shot 风格示例文本。

    上层可以直接把这个字符串拼进 prompt 的「以下是我过去真实说过的话」段落。

    Args:
        retriever: ``StyleRetriever`` 实例（或任何有 ``search`` 的对象）。
        query: 当前对方发来的话。为空时取索引里的前 ``top_k`` 条当兜底示例。
        top_k: 检索条数。
        min_score: 相似度下限。
        max_reply_chars: 传给展示层的兜底截断（索引通常已截断）。

    Returns:
        中文 few-shot 文本；没有可用示例时返回一句提示语而不是空串。
    """
    if retriever is None or not hasattr(retriever, "search"):
        return "（暂无可用风格示例）"
    results = retriever.search(query or "", top_k=top_k, min_score=min_score)
    if not results:
        # 兜底：用索引里时间最近的几条，保证 prompt 里永远有示例
        recs = getattr(retriever, "records", None) or []
        results = []
        for r in list(recs)[-max(1, top_k):]:
            rec = dict(r)
            rec["score"] = 0.0
            rec["reply_text"] = _truncate(str(rec.get("reply_text") or ""), max_reply_chars)
            results.append(rec)
    if not results:
        return "（暂无可用风格示例）"
    return format_examples(results, max_items=top_k, with_meta=True)


# --------------------------------------------------------------------------- #
# 7. Markdown 报告
# --------------------------------------------------------------------------- #

def _bar(count: int, max_count: int, width: int = 16) -> str:
    if max_count <= 0:
        return ""
    n = max(1, round(count / max_count * width))
    return "█" * n


def _as_float(v: Any, default: float = 0.0) -> float:
    """宽松转 float（报告渲染用，别让脏数据把写盘搞崩）。"""
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _fmt_top_table(rows: Sequence[Mapping[str, Any]], key: str, label: str,
                   value_label: str = "次数") -> list[str]:
    if not rows:
        return [f"（{label}：无）", ""]
    top = max(int(r.get(key) or 0) for r in rows) or 1
    lines = [f"| # | {label} | {value_label} | 分布 |", "| --- | --- | --- | --- |"]
    for i, r in enumerate(rows, 1):
        v = int(r.get(key) or 0)
        name = str(r.get("phrase") or r.get("emoji") or r.get("word") or "").replace("|", "\\|")
        lines.append(f"| {i} | `{name}` | {v} | {_bar(v, top)} |")
    lines.append("")
    return lines


def write_style_report(stats: dict, path: str | Path) -> None:
    """把 ``analyze_style`` 的结果写成人能读的中文 Markdown。

    Args:
        stats: ``analyze_style`` 的返回值。
        path: 目标 ``.md`` 路径（父目录会自动创建）。
    """
    stats = stats or {}
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)

    def g(*keys: str, default: Any = 0) -> Any:
        cur: Any = stats
        for k in keys:
            if not isinstance(cur, Mapping) or k not in cur:
                return default
            cur = cur[k]
        return cur

    L: list[str] = []
    L.append("# 我的聊天风格画像")
    L.append("")
    L.append("> 由 `luxun_qq.corpus.analyze_style` 自动生成；"
             "只统计我发出的文本消息（`Direction.OUT`）。")
    L.append("")

    # 1. 总览
    L.append("## 1. 总览")
    L.append("")
    L.append("| 指标 | 数值 |")
    L.append("| --- | --- |")
    L.append(f"| 文本消息数 | {int(g('count'))} |")
    L.append(f"| 总字数 | {int(g('total_chars'))} |")
    L.append(f"| 平均每条字数 | {g('avg_chars', default=0)} |")
    L.append(f"| 中位数 | {round(_as_float(g('median_chars')), 1)} |")
    L.append(f"| P90 | {round(_as_float(g('p90_chars')), 1)} |")
    L.append(f"| 最长 | {int(g('max_chars'))} |")
    L.append(f"| 每条的标点数 | {g('punctuation_per_msg', default=0)} |")
    L.append("")

    # 2. 碎句风格
    sp = g("message_splitting", default={}) or {}
    L.append("## 2. 碎句风格（message_splitting）")
    L.append("")
    if sp:
        L.append(f"- 我发言的「轮」数：**{sp.get('runs', 0)}**")
        L.append(f"- 每轮平均 **{_as_float(sp.get('avg_msgs_per_run')):.1f}** 条消息"
                 f"（中位数 {sp.get('median_msgs_per_run', 0)}，最多 {sp.get('max_msgs_per_run', 0)}）")
        L.append(f"- 拆成 ≥2 条发出的比例：**{_as_float(sp.get('multi_msg_run_ratio')):.1%}**")
        L.append(f"- 每条平均 **{_as_float(sp.get('avg_chars_per_msg')):.1f}** 字")
        L.append(f"- ≤6 字的短消息占比：**{_as_float(sp.get('short_msg_ratio')):.1%}**")
    else:
        L.append("（无数据）")
    L.append("")

    # 3. 标点
    L.append("## 3. 标点习惯（punctuation）")
    L.append("")
    punct = g("punctuation", default={}) or {}
    if punct:
        L.append("| 标点 | 次数 | 每 100 条出现 | 含该标点的消息数 |")
        L.append("| --- | --- | --- | --- |")
        for name, v in punct.items():
            L.append(f"| {name} | {v.get('count', 0)} | {v.get('per_100_msgs', 0)} "
                     f"| {v.get('messages_with', 0)} |")
    else:
        L.append("（无数据）")
    L.append("")

    # 4. emoji
    L.append("## 4. emoji / 颜文字 Top（emoji_usage）")
    L.append("")
    eu = g("emoji_usage", default={}) or {}
    L.append(f"- 总计 **{eu.get('total', 0)}** 次，去重 **{eu.get('distinct', 0)}** 种，"
             f"每 100 条消息 {eu.get('per_100_msgs', 0)} 次")
    L.append("")
    L.extend(_fmt_top_table(eu.get("top") or [], "count", "emoji/颜文字"))

    # 5. 口头禅
    L.append("## 5. 口头禅 / 高频短语 Top 30（catchphrases）")
    L.append("")
    L.append("> 统计口径：去空白后按标点切小句，取 2~5-gram；"
             "单条消息内同短语最多计 2 次，避免刷屏词霸榜。")
    L.append("")
    L.extend(_fmt_top_table(g("catchphrases", default=[]) or [], "count", "短语"))

    # 6. 笑法
    L.append("## 6. 笑法（laugh_style）")
    L.append("")
    la = g("laugh_style", default={}) or {}
    L.append(f"- 命中消息占比：**{la.get('ratio', 0):.1%}**"
             f"（{la.get('messages_with_laugh', 0)} 条 / 总 {int(g('count'))} 条）")
    kinds = la.get("kinds") or {}
    if kinds:
        L.append("- 分类计数：" + "、".join(f"{k} ×{v}" for k, v in kinds.items()))
    else:
        L.append("- 分类计数：（无）")
    L.append("")

    # 7. 脏话/锐评
    L.append("## 7. 脏话 / 锐评倾向（swear_or_sharp）")
    L.append("")
    sw = g("swear_or_sharp", default={}) or {}
    L.append(f"- 攻击性词命中总数 **{sw.get('total_hits', 0)}**，不同词 "
             f"**{sw.get('distinct', 0)}**，每千字 {sw.get('per_1000_chars', 0)}")
    if sw.get("top"):
        L.append("- Top 词：" + "、".join(f"{r['word']} ×{r['count']}" for r in sw["top"][:15]))
    else:
        L.append("- Top 词：（无）——我的语料里基本没有脏话，锐评靠逻辑不靠骂词")
    if sw.get("examples"):
        L.append("- 示例：")
        for e in sw["examples"][:5]:
            L.append(f"  - {e}")
    rh = sw.get("rhetoric") or {}
    if rh:
        L.append("")
        L.append(f"### 反语 / 文言标记（rhetoric）："
                 f"{rh.get('messages_with', 0)} 条消息里有，占 {rh.get('ratio', 0):.1%}")
        L.append("")
        if rh.get("top"):
            L.append("- 标记词：" + "、".join(
                f"{r['word']} ×{r['count']}" for r in rh["top"][:15]))
        else:
            L.append("- 标记词：（无）")
    L.append("")

    # 8. 开头词 / 结尾词
    L.append("## 8. 开头词 / 结尾词 Top 20（opener / closer）")
    L.append("")
    L.append("### 开头词")
    L.append("")
    L.extend(_fmt_top_table(g("opener", default=[]) or [], "count", "开头 2 字"))
    L.append("### 结尾词")
    L.append("")
    L.extend(_fmt_top_table(g("closer", default=[]) or [], "count", "结尾 2 字"))

    # 9. 活跃时段
    L.append("## 9. 活跃时段（active_hours）")
    L.append("")
    ah = g("active_hours", default={}) or {}
    hours = ah.get("hours") or [0] * 24
    mx = max(hours) if hours else 0
    if mx:
        L.append("| 小时 | 条数 | 分布 |")
        L.append("| --- | --- | --- |")
        for h, c in enumerate(hours):
            L.append(f"| {h:02d} | {c} | {_bar(int(c), int(mx))} |")
        L.append("")
        L.append(f"- 峰值小时：**{ah.get('peak_hour')}** 点（{ah.get('peak_count', 0)} 条）")
        gw = ah.get("golden_window")
        if gw:
            end = gw[1] if gw[1] > gw[0] else gw[1] + 24
            label = (f"{gw[0]:02d}:00 – {end:02d}:00"
                     if end <= 23 else f"{gw[0]:02d}:00 – 次日 {end - 24:02d}:00")
            L.append(f"- 4 小时黄金时段：**{label}**")
    else:
        L.append("（无数据）")
    L.append("")

    # 10. 局限
    L.append("## 10. 统计局限")
    L.append("")
    L.append("- **分词靠 2-gram**：不引入 jieba 等分词器，`catchphrases` 里会出现"
             "「不好」「什么」这类跨词边界的碎短语，需要人工过一眼。")
    L.append("- **停用词表是手写的**：偏 QQ 聊天语感，覆盖不了所有虚词。")
    L.append("- **情绪/笑法/脏话都是词表启发式**：反语（本项目的主角）必然误判。")
    L.append("- **只统计文本**：图片、语音、表情包不在统计内，会低估我的表达量。")
    L.append("- **时段按本地时区**：跨时区导出会偏。")
    L.append("")
    L.append("---")
    L.append("")
    L.append(f"*报告文件：`{p.name}`*")
    L.append("")

    p.write_text("\n".join(L), encoding="utf-8", newline="\n")
