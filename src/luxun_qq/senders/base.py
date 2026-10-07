# -*- coding: utf-8 -*-
"""发送层基类与公共落盘审计。

所有适配器共享同一套约定：

* ``send_text(peer_id, text) -> SendResult``  真实发送（或离线落盘）；
* ``send_candidates(peer_id, candidates, chosen)``  候选回复的选择/展示；
* 无论结果如何，都追加一条记录到 ``outbox_dir/*.jsonl``，便于审计「到底发了什么」。
"""
from __future__ import annotations

import json
import os
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

__all__ = [
    "SendResult",
    "SendError",
    "Sender",
    "append_outbox",
    "outbox_path",
]


class SendError(RuntimeError):
    """发送层统一异常：配置错误、协议错误、网络错误都用它，信息为中文。"""


@dataclass
class SendResult:
    """一次发送的结果。"""

    ok: bool
    adapter: str = ""
    detail: str = ""                       # 中文可读说明
    raw: dict[str, Any] = field(default_factory=dict)   # 原始响应 / 落盘记录

    # ---- 构造小工具 ----
    @classmethod
    def success(
        cls,
        adapter: str,
        detail: str = "",
        raw: Optional[dict[str, Any]] = None,
    ) -> "SendResult":
        return cls(ok=True, adapter=adapter, detail=detail, raw=raw or {})

    @classmethod
    def failure(
        cls,
        adapter: str,
        detail: str,
        raw: Optional[dict[str, Any]] = None,
    ) -> "SendResult":
        return cls(ok=False, adapter=adapter, detail=detail, raw=raw or {})

    def __bool__(self) -> bool:  # 方便 `if sender.send_text(...):`
        return self.ok

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "adapter": self.adapter,
            "detail": self.detail,
            "raw": self.raw,
        }


def outbox_path(outbox_dir: Path, adapter: str) -> Path:
    """按日期分文件的 outbox 路径：``outbox/napcat-2025-01-31.jsonl``。"""
    day = datetime.now().strftime("%Y-%m-%d")
    return Path(outbox_dir) / f"{adapter or 'unknown'}-{day}.jsonl"


def append_outbox(outbox_dir: Path, record: dict[str, Any]) -> Path:
    """把一条记录追加到 outbox（JSONL，UTF-8，不转义中文），返回文件路径。

    落盘失败不抛异常（发送已经发生或已经失败，审计日志不该阻断主流程），
    但会把失败原因塞进记录的 ``_record_error`` 字段里。
    """
    adapter = str(record.get("adapter") or "unknown")
    path = outbox_path(outbox_dir, adapter)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except OSError as exc:  # pragma: no cover - 磁盘异常
        record["_record_error"] = f"outbox 写入失败：{exc}"
    return path


class Sender(ABC):
    """发送适配器抽象基类。"""

    #: 适配器名，会被写进 SendResult.adapter 与 outbox 文件名
    name: str = "base"

    def __init__(self, outbox_dir: Path | str, *, timeout: int = 20) -> None:
        self.outbox_dir = Path(outbox_dir)
        self.timeout = int(timeout)
        self.outbox_dir.mkdir(parents=True, exist_ok=True)

    # ---- 子类必须实现 ----
    @abstractmethod
    def send_text(self, peer_id: str, text: str) -> SendResult:
        """把一段文本发给 ``peer_id``（QQ 号 / 会话标识）。"""
        raise NotImplementedError

    # ---- 通用默认实现 ----
    def send_candidates(
        self,
        peer_id: str,
        candidates: list[str],
        chosen: Optional[int] = None,
    ) -> SendResult:
        """处理三步思考产出的候选回复。

        * ``chosen=None``：只展示不发送（人在环模式），但仍写 outbox 记录；
        * ``chosen`` 为下标：调用 ``send_text`` 真正发出该条。
        """
        items = [str(c) for c in (candidates or [])]
        if not items:
            return SendResult.failure(self.name, "候选回复为空，没有可发送的内容。")

        if chosen is None:
            detail = f"仅展示 {len(items)} 条候选，未发送（chosen=None）。"
            payload = {
                "action": "show_candidates",
                "peer_id": str(peer_id),
                "chosen": None,
                "candidates": items,
                "sent": False,
            }
            path = self.record(
                "show_candidates",
                peer_id,
                text="",
                ok=True,
                detail=detail,
                raw=payload,
            )
            return SendResult.success(
                self.name,
                f"{detail}记录：{path}",
                payload,
            )

        idx = int(chosen)
        if idx < 0 or idx >= len(items):
            return SendResult.failure(
                self.name,
                f"chosen={idx} 越界：候选共 {len(items)} 条，合法下标 0~{len(items) - 1}。",
                {"candidates": items, "chosen": idx},
            )
        result = self.send_text(peer_id, items[idx])
        # 附带候选上下文，便于回溯「为什么发的是这句」
        result.raw.setdefault("candidates", items)
        result.raw.setdefault("chosen", idx)
        return result

    # ---- 供子类复用的落盘 ----
    def record(
        self,
        action: str,
        peer_id: str,
        text: str = "",
        ok: bool = True,
        detail: str = "",
        raw: Optional[dict[str, Any]] = None,
        **extra: Any,
    ) -> Path:
        """统一的 outbox 记录写入。"""
        rec: dict[str, Any] = {
            "ts": int(time.time()),
            "time": datetime.now().isoformat(timespec="seconds"),
            "pid": os.getpid(),
            "adapter": self.name,
            "action": action,
            "peer_id": str(peer_id),
            "text": text,
            "ok": bool(ok),
            "detail": detail,
            "raw": raw or {},
        }
        rec.update(extra)
        return append_outbox(self.outbox_dir, rec)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<{type(self).__name__} name={self.name!r} outbox={str(self.outbox_dir)!r}>"
