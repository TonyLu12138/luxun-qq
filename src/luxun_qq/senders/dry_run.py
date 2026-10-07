# -*- coding: utf-8 -*-
"""dry_run 适配器：默认选项，不联网、只落盘。

用于开发与自检：真实的调用链（三步思考 -> 选择 -> 发送）都能跑通，
但消息只写进 ``data/outbox/*.jsonl``，不会打扰任何人。
"""
from __future__ import annotations

from .base import SendResult, Sender

__all__ = ["DryRunSender"]


class DryRunSender(Sender):
    """只记录不发送。永远返回成功（除非落盘本身出错）。"""

    name = "dry_run"

    def send_text(self, peer_id: str, text: str) -> SendResult:
        detail = "dry_run：仅落盘，未真实发送"
        payload = {
            "action": "send_text",
            "peer_id": str(peer_id),
            "text": text,
            "sent": False,
            "note": "dry_run：未真实发送",
        }
        path = self.record("send_text", peer_id, text=text, ok=True, detail=detail, raw=payload)
        # 控制台回显，方便人工确认内容
        print(f"[dry_run] -> {peer_id}: {text}")
        return SendResult.success(
            self.name,
            f"dry_run 已记录（未发送）。记录文件：{path}",
            payload,
        )
