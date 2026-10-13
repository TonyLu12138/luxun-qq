"""One receiver per connection; API responses are routed by echo to callers."""
from __future__ import annotations

import json
import queue
import threading
import uuid
from typing import Callable

from ..senders.base import SendError
from ..senders.ws import WsSender


class OneBotConnection:
    def __init__(self, url: str, token: str, on_event: Callable,
                 on_disconnect: Callable):
        self.url = url
        self.token = token
        self.on_event = on_event
        self.on_disconnect = on_disconnect
        self.socket = None
        self.closed = threading.Event()
        self.waiters: dict[str, queue.Queue] = {}
        self.lock = threading.Lock()
        self.reader = None

    def connect(self) -> None:
        import websocket
        self.socket = websocket.create_connection(
            self.url, timeout=10, enable_multithread=True,
            header=[f"Authorization: Bearer {self.token}"] if self.token else [])
        # A dedicated reader may block indefinitely; shutdown() interrupts it.
        # Per-call deadlines are independent and enforced on each response queue.
        self.socket.settimeout(None)
        if self.closed.is_set():
            self.socket.shutdown()
            raise SendError("连接已取消")
        self.reader = threading.Thread(target=self._receive, name="onebot-reader", daemon=True)
        self.reader.start()

    def _route(self, message: dict) -> None:
        if "echo" in message:
            with self.lock:
                waiter = self.waiters.get(str(message["echo"]))
            if waiter is not None:
                waiter.put(message)
        elif message.get("post_type") == "message":
            self.on_event(message)

    def _receive(self) -> None:
        try:
            while not self.closed.is_set():
                text = self.socket.recv()
                if not text:
                    raise SendError("QQ 连接已断开")
                try:
                    message = json.loads(text)
                except (ValueError, TypeError):
                    continue
                if isinstance(message, dict):
                    self._route(message)
        except Exception:
            if not self.closed.is_set():
                self.on_disconnect("QQ 连接已断开，请检查组件后重新启动。")
        finally:
            self.close()

    def call(self, action: str, params: dict, timeout: float = 20) -> dict:
        echo = uuid.uuid4().hex
        waiter: queue.Queue = queue.Queue()
        with self.lock:
            if self.closed.is_set() or self.socket is None:
                raise SendError("QQ 尚未连接")
            self.waiters[echo] = waiter
        try:
            self.socket.send(json.dumps({"action": action, "params": params, "echo": echo},
                                        ensure_ascii=False))
            try:
                response = waiter.get(timeout=timeout)
            except queue.Empty as exc:
                raise SendError("QQ 响应超时；发送结果未知，请先检查聊天记录，勿重复发送。") from exc
            if isinstance(response, Exception):
                raise response
            return response
        except (OSError, ConnectionError) as exc:
            raise SendError("QQ 连接不可用") from exc
        finally:
            with self.lock:
                self.waiters.pop(echo, None)

    def close(self) -> None:
        self.closed.set()
        with self.lock:
            waiters = list(self.waiters.values())
        for waiter in waiters:
            waiter.put(SendError("QQ 连接已关闭；在途消息结果可能未知。"))
        if self.socket is not None:
            try:
                self.socket.shutdown()
            except Exception:
                pass


class DesktopSender(WsSender):
    def __init__(self, connection: OneBotConnection, outbox_dir):
        super().__init__(connection, outbox_dir)

    def call(self, action: str, params: dict) -> dict:
        return self.client.call(action, params, self.timeout)
