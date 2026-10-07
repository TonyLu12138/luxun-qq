# -*- coding: utf-8 -*-
"""luxun-qq 数据契约层。

所有模块（解密导出、语料构建、检索、训练、推理、发送）共享这里的消息模型，
保证任何数据源都归一化成同一个形状。
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Iterable, Iterator, Optional

SCHEMA_VERSION = "1.0"


class Direction(str, Enum):
    """消息方向，相对「我自己」而言。"""

    OUT = "out"      # 我发出的
    IN = "in"        # 对方发来的

    @property
    def is_mine(self) -> bool:
        return self is Direction.OUT


class MsgKind(str, Enum):
    """归一化后的消息种类。"""

    TEXT = "text"
    IMAGE = "image"
    VIDEO = "video"
    FILE = "file"
    STICKER = "sticker"     # 表情 / 表情包
    VOICE = "voice"         # 语音
    REPLY = "reply"         # 引用回复
    FORWARD = "forward"     # 合并转发
    CALL = "call"           # 语音/视频通话
    SYSTEM = "system"       # 系统提示
    MIXED = "mixed"         # 多段混合
    UNKNOWN = "unknown"


@dataclass
class Message:
    """一条归一化消息。"""

    msg_id: str
    ts: int                              # Unix 秒
    direction: Direction
    peer_id: str                         # 会话对象（对方 QQ 号）
    text: str = ""                       # 纯文本；非文本消息为空串
    kind: MsgKind = MsgKind.TEXT
    sender_id: str = ""                  # 发送者 QQ 号
    raw: dict[str, Any] = field(default_factory=dict)   # 原始内容，便于回溯
    source: str = ""                     # 数据来源标记，如 "nt_msg.db"

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["direction"] = self.direction.value
        d["kind"] = self.kind.value
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Message":
        return cls(
            msg_id=str(d["msg_id"]),
            ts=int(d["ts"]),
            direction=Direction(d["direction"]),
            peer_id=str(d["peer_id"]),
            text=d.get("text") or "",
            kind=MsgKind(d.get("kind") or "text"),
            sender_id=str(d.get("sender_id") or ""),
            raw=d.get("raw") or {},
            source=d.get("source") or "",
        )

    def to_jsonl(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)


@dataclass
class Exchange:
    """一个「对方说 → 我说」的问答对，风格学习与 few-shot 检索的最小单元。"""

    peer_id: str
    incoming: list[Message]      # 触发我的上下文（对方的话，可能连续多条）
    reply: list[Message]         # 我的回应（可能连续多条）
    ts: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "peer_id": self.peer_id,
            "ts": self.ts,
            "incoming": [m.to_dict() for m in self.incoming],
            "reply": [m.to_dict() for m in self.reply],
        }


def write_jsonl(path: str, items: Iterable[Any]) -> int:
    """把 Message / Exchange / dict 写成 JSONL，返回条数。"""
    n = 0
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        for it in items:
            if hasattr(it, "to_jsonl"):
                f.write(it.to_jsonl() + "\n")
            elif hasattr(it, "to_dict"):
                f.write(json.dumps(it.to_dict(), ensure_ascii=False) + "\n")
            else:
                f.write(json.dumps(it, ensure_ascii=False) + "\n")
            n += 1
    return n


def read_jsonl(path: str) -> Iterator[dict[str, Any]]:
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                yield json.loads(line)


def read_messages(path: str) -> list[Message]:
    return [Message.from_dict(d) for d in read_jsonl(path)]
