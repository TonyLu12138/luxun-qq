# -*- coding: utf-8 -*-
"""把 OneBot 消息段翻译成「人能读懂的文字」。

解决的问题：
    之前 ``_extract_text`` 只取 ``text`` 段，于是对方发一个表情包，
    服务认为"没有文本"，直接把整条消息丢掉（``缺少发送者或文本``）。
    可表情包恰恰是聊天里情绪信息最密集的部分。

现在支持的段类型：

    text     直接取文本
    face     QQ 内置小黄脸      -> （笑哭）        ← 内置 399 个 id 对照表
    mface    商城表情包         -> （表情：熊猫头）
    bface    超级表情           -> （超级表情：...）
    image    图片/表情包        -> （发了张图）或 OCR/summary 里的文字
    at       @某人              -> @某人
    reply    引用回复           -> 保留（用 [回复] 标记）
    json/xml 卡片消息           -> 尽量摘出标题
    其他     未知类型           -> （[类型]）兜底，绝不静默丢弃

自学习：
    内置表没命中时，优先用事件自带的 ``summary``（QQ 客户端给的名字），
    并把它写进 SQLite 缓存 —— 用得越久，认得越全。
"""
from __future__ import annotations

from typing import Any, Optional

from .faces_data import FACE_NAMES

__all__ = ["face_name", "describe_segment", "segments_to_text",
           "MessageText", "FACE_NAMES", "is_meaningful"]

#: 这些段类型不含语义，翻译后若只剩它们，则视作"没有实质内容"
_EMPTY_TYPES = {"reply"}


class MessageText:
    """解析结果：既有给模型看的文本，也有结构化信息供后续判断。"""

    __slots__ = ("text", "has_content", "kinds", "face_ids")

    def __init__(self, text: str, has_content: bool, kinds: list[str],
                 face_ids: list[str]):
        self.text = text
        self.has_content = has_content
        self.kinds = kinds
        self.face_ids = face_ids

    def __str__(self) -> str:            # 方便直接当字符串用
        return self.text

    def __bool__(self) -> bool:
        return bool(self.text.strip())

    def __repr__(self) -> str:           # pragma: no cover
        return (f"<MessageText {self.text!r} kinds={self.kinds} "
                f"content={self.has_content}>")


def face_name(face_id: Any, summary: str = "") -> str:
    """取 QQ 表情的名字。

    优先级：事件自带的 summary > 内置对照表 > 通用兜底。

    Args:
        face_id: OneBot ``face`` 段的 ``id``（也可能是 raw 字符串）
        summary: 事件里自带的名称，形如 ``[笑哭]`` 或 ``/笑哭``
    """
    # 1) summary 最可信（QQ 客户端自己给的）
    if summary:
        nm = summary.strip().strip("[]【】").lstrip("/").strip()
        if nm:
            return nm

    fid = str(face_id or "").strip()
    if not fid:
        return "表情"
    # 有的实现把 raw 名字塞在 id 里（如 "/笑哭"）
    if not fid.isdigit():
        nm = fid.strip("/").strip("[]")
        return nm or "表情"
    # 2) 内置表
    nm = FACE_NAMES.get(fid)
    if nm:
        return nm
    # 3) 兜底：保留 id，方便后续在缓存里补上名字
    return f"表情{fid}"


def _clean(text: Any) -> str:
    return str(text or "").replace("\r", " ").replace("\n", " ").strip()


def describe_segment(seg: dict[str, Any], *,
                     learn: Optional[dict[str, str]] = None) -> str:
    """把一个 OneBot 消息段翻译成文字。

    Args:
        learn: 传入一个 dict 时，遇到新表情会把 ``{id: name}`` 写进去，
               由调用方持久化（自学习）。
    """
    if not isinstance(seg, dict):
        return ""
    stype = str(seg.get("type") or "")
    data = seg.get("data")
    if not isinstance(data, dict):
        data = {}

    if stype == "text":
        return _clean(data.get("text"))

    if stype == "face":
        fid = data.get("id") or data.get("raw") or ""
        summary = _clean(data.get("summary") or data.get("raw") or "")
        nm = face_name(fid, summary)
        fid_s = str(fid).strip()
        # 只有名字确实来自事件（summary / raw）时才值得记进缓存；
        # 否则会把 "表情8888" 这种兜底名当成学到的知识存下来。
        from_event = bool(summary.strip().strip("[]【】").lstrip("/").strip())
        if (learn is not None and fid_s.isdigit()
                and fid_s not in FACE_NAMES and from_event):
            learn[fid_s] = nm
        return f"（{nm}）"

    if stype == "mface":
        nm = _clean(data.get("summary") or data.get("name")
                    or data.get("faceName") or "")
        nm = nm.strip("[]【】").lstrip("/")
        return f"（表情包：{nm}）" if nm else "（发了个表情包）"

    if stype == "bface":
        nm = _clean(data.get("summary") or data.get("name") or "")
        nm = nm.strip("[]【】").lstrip("/")
        return f"（超级表情：{nm}）" if nm else "（发了个超级表情）"

    if stype == "image":
        # summary 常见值："[动画表情]"、"[图片]"、"你礼貌吗"
        nm = _clean(data.get("summary") or data.get("name") or "")
        nm = nm.strip("[]【】")
        if nm and nm not in ("图片", "动画表情", "表情"):
            # summary 里有实际文字（很多表情包带字），信息量最高
            return f"（图片，上面写着「{nm}」）"
        if nm == "动画表情":
            return "（发了个表情包）"
        return "（发了张图）"

    if stype == "record":
        return "（发了段语音）"
    if stype == "video":
        return "（发了个视频）"
    if stype == "file":
        return f"（发了个文件：{_clean(data.get('name') or data.get('file'))}）"

    if stype == "at":
        who = _clean(data.get("name") or data.get("qq")
                     or data.get("user_id") or "")
        return f"@{who}" if who else "@某人"

    if stype == "reply":
        # 引用回复：内容本身不提供语义，标记一下让模型知道在回应某条
        return "[引用了一条消息]"

    if stype == "json":
        raw = _clean(data.get("data") or "")
        return _card_title(raw, "卡片消息")
    if stype == "xml":
        raw = _clean(data.get("data") or "")
        return _card_title(raw, "卡片消息")

    if stype == "poke":
        return "（戳了戳）"
    if stype == "gift":
        return "（送了个礼物）"

    # 未知类型也不静默丢弃——否则又会退回"消息被当成空的丢掉"那个 bug
    return f"（[{stype or '未知'}]）" if stype else ""


def _card_title(raw: str, fallback: str) -> str:
    """从 json/xml 卡片里尽量摘出标题，摘不到就返回兜底文案。"""
    import re

    if not raw:
        return f"（{fallback}）"
    for pat in (r'"title"\s*:\s*"([^"]{2,40})"',
                r'<title[^>]*>([^<]{2,40})</title>',
                r'"desc"\s*:\s*"([^"]{2,40})"'):
        m = re.search(pat, raw)
        if m:
            return f"（{m.group(1)}）"
    return f"（{fallback}）"


def is_meaningful(text: str) -> bool:
    """判断解析结果里有没有实质内容（全是（发了张图）这类也算有）。"""
    t = (text or "").strip()
    if not t:
        return False
    # 只有引用标记不算内容
    stripped = t.replace("[引用了一条消息]", "").strip()
    return bool(stripped) or "引用" not in t


def segments_to_text(message: Any, *,
                     learn: Optional[dict[str, str]] = None) -> MessageText:
    """把 OneBot 的 message 字段（数组或字符串）翻成文字。

    返回 :class:`MessageText`：既给出文本，也给出"是否含实质内容"、
    出现过哪些段类型、用到了哪些表情 id（供上层判断与学习）。
    """
    kinds: list[str] = []
    face_ids: list[str] = []
    parts: list[str] = []

    if isinstance(message, str):
        return MessageText(_clean(message), bool(_clean(message)),
                           ["text"], [])

    if isinstance(message, dict):
        message = [message]

    if not isinstance(message, list):
        return MessageText(_clean(message), False, [], [])

    for seg in message:
        if not isinstance(seg, dict):
            continue
        stype = str(seg.get("type") or "")
        if stype:
            kinds.append(stype)
        if stype == "face":
            d = seg.get("data") or {}
            fid = str(d.get("id") or "").strip()
            if fid:
                face_ids.append(fid)
        piece = describe_segment(seg, learn=learn)
        if piece:
            parts.append(piece)

    text = " ".join(p for p in parts if p).strip()
    text = " ".join(text.split())          # 压掉多余空白
    has = bool(text) and not all(
        k in _EMPTY_TYPES for k in kinds) if kinds else bool(text)
    return MessageText(text, has, kinds, face_ids)
