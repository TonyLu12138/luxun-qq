# -*- coding: utf-8 -*-
"""通过 OneBot WebSocket 连接发送消息的适配器。

与 napcat.py（HTTP 版）的区别：
    - HTTP 版：每次 send 向 NapCat 的 HTTP 端口 POST 一个 API 请求
    - WS 版：在同一条 WebSocket 连接上发 action 帧，按 echo 字段等响应

WS 版的优势是**收事件与发消息共用一条连接**，省掉 HTTP 事件上报那一层，
因此不受 NapCat 的 httpClient 上报行为影响。
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

from ..wsclient import WSClient, WSError
from .base import SendError, SendResult, Sender

__all__ = ["WsSender"]


class WsSender(Sender):
    """OneBot 11 WebSocket 发送器。"""

    name = "ws"

    def __init__(self, client: WSClient, outbox_dir: Path | str, *,
                 self_id: str = "", timeout: int = 20) -> None:
        super().__init__(outbox_dir, timeout=timeout)
        self.client = client
        self.self_id = str(self_id or "")
        self._echo_seq = 0
        self._echo_lock = threading.Lock()
        #: 等待 API 响应期间被夹带收到的事件帧（交给事件循环处理）
        self._deferred: list[dict] = []

    # ------------------------------------------------------------ 低层调用
    def _next_echo(self) -> str:
        with self._echo_lock:
            self._echo_seq += 1
            return f"luxun-{self._echo_seq}-{int(time.time() * 1000)}"

    def call(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        """发一个 OneBot action 并等待它的响应。

        等待期间可能收到事件推送（同一条连接双工），这些帧会被暂存到
        ``_deferred``，由事件循环稍后取走处理，不会丢也不会错配。
        """
        echo = self._next_echo()
        try:
            self.client.send_json({"action": action, "params": params, "echo": echo})
        except (WSError, OSError) as e:
            raise SendError(f"WebSocket 发送失败：{e}") from e

        deadline = time.time() + self.timeout
        while time.time() < deadline:
            try:
                text = self.client.recv()
            except Exception as e:
                raise SendError(f"WebSocket 读取响应失败：{e}") from e
            if text is None:
                raise SendError("WebSocket 连接已关闭")
            try:
                msg = json.loads(text)
            except Exception:
                continue
            if not isinstance(msg, dict):
                continue
            if str(msg.get("echo") or "") == echo:
                return msg
            # 不是我们的响应：多半是事件，暂存后交给事件循环
            self._deferred.append(msg)
            if len(self._deferred) > 500:
                self._deferred = self._deferred[-200:]
        raise SendError(f"{action} 等待响应超时（{self.timeout}s）")

    def take_deferred(self) -> list[dict]:
        """取走等待响应期间夹带收到的事件。"""
        with self._echo_lock:
            items = self._deferred
            self._deferred = []
        return items

    # ------------------------------------------------------------ Sender 接口
    def send_text(self, peer_id: str, text: str) -> SendResult:
        try:
            resp = self.call("send_private_msg",
                             {"user_id": int(peer_id), "message": text})
        except (SendError, ValueError) as e:
            self.record("send_private_msg", peer_id, text=text, ok=False,
                        detail=str(e), raw={"error": str(e)})
            return SendResult.failure(self.name, str(e), {"error": str(e)})

        ok = resp.get("status") == "ok" and int(resp.get("retcode", -1)) == 0
        detail = str(resp.get("message") or resp.get("wording") or "")
        self.record("send_private_msg", peer_id, text=text, ok=ok,
                    detail=detail, raw=resp)
        if not ok:
            return SendResult.failure(
                self.name, f"NapCat 返回失败：{detail or resp}", resp)
        return SendResult.success(self.name, detail or "ok", resp)

    # ------------------------------------------------------------ 额外能力
    def get_login_info(self) -> dict[str, Any]:
        data = self.call("get_login_info", {}).get("data")
        return data if isinstance(data, dict) else {}
