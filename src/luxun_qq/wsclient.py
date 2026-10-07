# -*- coding: utf-8 -*-
"""零依赖的 OneBot 11 WebSocket 客户端（手写 RFC 6455）。

为什么自己写：
    本机环境装第三方包很不稳定（pip 多次超时），而 WebSocket 协议本身不复杂。
    这里只实现我们需要的最小子集：文本帧、掩码、分片重组、ping/pong、关闭握手。

用途：连接 NapCat 的 WebSocket **服务器**，双向使用同一条连接：
    - 收事件：NapCat 推送 message / notice / meta_event
    - 调 API：发 send_private_msg 等，按 echo 字段匹配响应
"""
from __future__ import annotations

import base64
import json
import os
import socket
import ssl
import struct
import threading
import time
import urllib.parse
from typing import Any, Callable, Optional

__all__ = ["WSClient", "WSError"]

_MAGIC = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

# opcode
OP_CONT = 0x0
OP_TEXT = 0x1
OP_BIN = 0x2
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA


class WSError(RuntimeError):
    pass


class WSClient:
    """极简 WebSocket 客户端。

    用法::

        ws = WSClient("ws://127.0.0.1:3001")
        ws.connect()
        ws.send_text('{"action":"get_login_info","echo":"1"}')
        frame = ws.recv()          # 阻塞读取一条文本消息
        ws.close()
    """

    def __init__(self, url: str, *, token: str = "", timeout: float = 20.0,
                 headers: Optional[dict] = None):
        self.url = url
        self.token = token
        self.timeout = timeout
        self.extra_headers = headers or {}
        self.sock: Optional[socket.socket] = None
        self._buf = b""
        self._lock = threading.Lock()

    # ---------------------------------------------------------------- 连接
    def connect(self) -> None:
        u = urllib.parse.urlparse(self.url)
        secure = u.scheme in ("wss", "https")
        host = u.hostname or "127.0.0.1"
        port = u.port or (443 if secure else 80)
        path = u.path or "/"
        if u.query:
            path += "?" + u.query

        raw = socket.create_connection((host, port), timeout=self.timeout)
        if secure:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            raw = ctx.wrap_socket(raw, server_hostname=host)
        raw.settimeout(self.timeout)
        self.sock = raw

        key = base64.b64encode(os.urandom(16)).decode()
        lines = [
            f"GET {path} HTTP/1.1",
            f"Host: {host}:{port}",
            "Upgrade: websocket",
            "Connection: Upgrade",
            f"Sec-WebSocket-Key: {key}",
            "Sec-WebSocket-Version: 13",
        ]
        if self.token:
            lines.append(f"Authorization: Bearer {self.token}")
        for k, v in self.extra_headers.items():
            lines.append(f"{k}: {v}")
        raw.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())

        # 读响应头
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = raw.recv(4096)
            if not chunk:
                raise WSError("握手时连接被关闭")
            head += chunk
        header_part, _, rest = head.partition(b"\r\n\r\n")
        status_line = header_part.split(b"\r\n", 1)[0].decode("latin-1")
        if "101" not in status_line:
            raise WSError(f"WebSocket 握手失败：{status_line}")
        # 校验 accept
        accept = None
        for line in header_part.split(b"\r\n")[1:]:
            if b":" in line:
                k, v = line.split(b":", 1)
                if k.strip().lower() == b"sec-websocket-accept":
                    accept = v.strip().decode()
        expect = base64.b64encode(
            __import__("hashlib").sha1((key + _MAGIC).encode()).digest()).decode()
        if accept and accept != expect:
            raise WSError("Sec-WebSocket-Accept 校验失败")
        self._buf = rest

    # ---------------------------------------------------------------- 帧
    def _recv_exact(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = self.sock.recv(max(4096, n - len(self._buf)))
            if not chunk:
                raise WSError("连接已关闭")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def _read_frame(self) -> tuple[int, bytes]:
        b1, b2 = self._recv_exact(2)
        fin = b1 & 0x80
        opcode = b1 & 0x0F
        masked = b2 & 0x80
        length = b2 & 0x7F
        if length == 126:
            length = struct.unpack(">H", self._recv_exact(2))[0]
        elif length == 127:
            length = struct.unpack(">Q", self._recv_exact(8))[0]
        mask = self._recv_exact(4) if masked else b""
        payload = self._recv_exact(length) if length else b""
        if masked:
            payload = bytes(payload[i] ^ mask[i % 4] for i in range(len(payload)))
        # 分片重组
        if not fin and opcode in (OP_TEXT, OP_BIN):
            parts = [payload]
            while True:
                b1n, _b2n = self._recv_exact(2)
                op_n = b1n & 0x0F
                ln = _b2n & 0x7F
                if ln == 126:
                    ln = struct.unpack(">H", self._recv_exact(2))[0]
                elif ln == 127:
                    ln = struct.unpack(">Q", self._recv_exact(8))[0]
                m = self._recv_exact(4) if (_b2n & 0x80) else b""
                p = self._recv_exact(ln) if ln else b""
                if m:
                    p = bytes(p[i] ^ m[i % 4] for i in range(len(p)))
                if op_n == OP_CONT:
                    parts.append(p)
                if b1n & 0x80:
                    break
            payload = b"".join(parts)
            opcode = OP_TEXT
        return opcode, payload

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        header = bytearray()
        header.append(0x80 | opcode)          # FIN + opcode
        n = len(payload)
        if n < 126:
            header.append(0x80 | n)
        elif n < (1 << 16):
            header.append(0x80 | 126)
            header += struct.pack(">H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", n)
        mask = os.urandom(4)
        header += mask
        masked = bytes(payload[i] ^ mask[i % 4] for i in range(n))
        with self._lock:
            self.sock.sendall(bytes(header) + masked)

    def send_text(self, text: str) -> None:
        self._send_frame(OP_TEXT, text.encode("utf-8"))

    def send_json(self, obj: Any) -> None:
        self.send_text(json.dumps(obj, ensure_ascii=False))

    def recv(self) -> Optional[str]:
        """读一条文本消息；返回 None 表示连接关闭。

        自动处理 ping/pong：收到 ping 立即回 pong。
        """
        while True:
            op, payload = self._read_frame()
            if op == OP_TEXT:
                return payload.decode("utf-8", "ignore")
            if op == OP_BIN:
                return payload.decode("utf-8", "ignore")
            if op == OP_PING:
                self._send_frame(OP_PONG, payload)
                continue
            if op == OP_PONG:
                continue
            if op == OP_CLOSE:
                try:
                    self._send_frame(OP_CLOSE, b"")
                except Exception:
                    pass
                return None

    def close(self) -> None:
        if not self.sock:
            return
        try:
            self._send_frame(OP_CLOSE, struct.pack(">H", 1000))
        except Exception:
            pass
        try:
            self.sock.close()
        except Exception:
            pass
        self.sock = None

    # ---------------------------------------------------------------- 上下文
    def __enter__(self) -> "WSClient":
        self.connect()
        return self

    def __exit__(self, *exc) -> None:
        self.close()
