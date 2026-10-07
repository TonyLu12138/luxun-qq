# -*- coding: utf-8 -*-
"""假 NapCat 服务：用 ``http.server`` 模拟 OneBot 11 的最小接口。

用途：在没有真 NapCat 的环境里端到端验证发送链路
（``NapCatSender`` -> HTTP -> 服务端落盘）。

实现接口：

* ``POST /send_private_msg``  body ``{"user_id": int, "message": str}``
* ``POST /send_group_msg``    body ``{"group_id": int, "message": str}``
* ``POST /get_login_info``

把收到的消息打印到控制台并追加到 ``data/outbox/echo_server.jsonl``。

用法::

    python -m luxun_qq.senders.echo_server            # 默认 3000 端口
    python -m luxun_qq.senders.echo_server 3001       # 指定端口
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional

__all__ = ["EchoServerHandler", "EchoServer", "main", "DEFAULT_PORT", "DEFAULT_SELF_ID"]

DEFAULT_PORT = 3000
DEFAULT_SELF_ID = "10000"


class EchoServer:
    """假 NapCat 服务的状态与落盘逻辑。"""

    def __init__(
        self,
        log_path: Path | str,
        *,
        self_id: str = DEFAULT_SELF_ID,
        access_token: str = "",
        echo_stdout: bool = True,
    ) -> None:
        self.log_path = Path(log_path)
        self.self_id = str(self_id)
        self.access_token = access_token or ""
        self.echo_stdout = echo_stdout
        self.log_path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, record: dict[str, Any]) -> None:
        """追加一条记录到 jsonl。"""
        with open(self.log_path, "a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")

    def handle(self, action: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        """处理一个 OneBot action，返回 ``(http_status, 响应体)``。"""
        ts = int(time.time())
        record: dict[str, Any] = {
            "ts": ts,
            "time": datetime.now().isoformat(timespec="seconds"),
            "action": action,
            "request": body,
            "server": "fake-napcat",
        }

        if action == "get_login_info":
            record.update({"ok": True, "peer_id": self.self_id})
            self.append(record)
            if self.echo_stdout:
                print(f"[echo_server] get_login_info -> user_id={self.self_id}")
            return 200, {
                "status": "ok",
                "retcode": 0,
                "data": {"user_id": _to_int_or(self.self_id, 0), "nickname": "假NapCat"},
            }

        if action == "send_private_msg":
            raw_uid = body.get("user_id")
            message = str(body.get("message") or "")
            try:
                user_id = int(str(raw_uid).strip())
            except (TypeError, ValueError):
                record["ok"] = False
                record["peer_id"] = str(raw_uid)
                record["error"] = f"user_id 非法：{raw_uid!r}"
                self.append(record)
                return 200, {
                    "status": "failed",
                    "retcode": 100,
                    "message": "user_id 必须是整数",
                    "wording": "user_id 必须是整数",
                    "data": None,
                }
            record.update({"ok": True, "peer_id": str(user_id), "user_id": user_id, "message": message})
            self.append(record)
            if self.echo_stdout:
                print(f"[echo_server] 私聊 -> {user_id}: {message}")
            return 200, {
                "status": "ok",
                "retcode": 0,
                "data": {"message_id": _next_message_id()},
            }

        if action == "send_group_msg":
            raw_gid = body.get("group_id")
            message = str(body.get("message") or "")
            try:
                group_id = int(str(raw_gid).strip())
            except (TypeError, ValueError):
                record["ok"] = False
                record["peer_id"] = str(raw_gid)
                record["error"] = f"group_id 非法：{raw_gid!r}"
                self.append(record)
                return 200, {
                    "status": "failed",
                    "retcode": 100,
                    "message": "group_id 必须是整数",
                    "data": None,
                }
            record.update({"ok": True, "peer_id": str(group_id), "group_id": group_id, "message": message})
            self.append(record)
            if self.echo_stdout:
                print(f"[echo_server] 群聊 -> {group_id}: {message}")
            return 200, {
                "status": "ok",
                "retcode": 0,
                "data": {"message_id": _next_message_id()},
            }

        record["ok"] = False
        record["error"] = f"未实现的 action：{action}"
        self.append(record)
        return 404, {
            "status": "failed",
            "retcode": 1404,
            "message": f"未实现的 action：{action}",
            "data": None,
        }


_MSG_ID = [1000]


def _next_message_id() -> int:
    _MSG_ID[0] += 1
    return _MSG_ID[0]


def _to_int_or(value: Any, fallback: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return fallback


class EchoServerHandler(BaseHTTPRequestHandler):
    """把 HTTP 请求翻译成 ``EchoServer.handle`` 调用。"""

    server_version = "FakeNapCat/0.1"
    protocol_version = "HTTP/1.1"

    # 由 make_server 注入
    echo: EchoServer

    def do_GET(self) -> None:  # noqa: N802 - http.server 的命名约定
        if self.path.rstrip("/") in ("", "/health", "/ping"):
            self._send_json(200, {"status": "ok", "retcode": 0, "data": {"fake": "napcat"}})
            return
        self._send_json(404, {"status": "failed", "retcode": 1404, "data": None})

    def do_POST(self) -> None:  # noqa: N802
        # 可选 token 校验：配置了 token 才检查（和真 NapCat 行为一致）
        token = self.echo.access_token
        if token:
            auth = self.headers.get("Authorization") or ""
            if auth != f"Bearer {token}":
                self._send_json(
                    401,
                    {"status": "failed", "retcode": 1401, "message": "access_token 不匹配", "data": None},
                )
                return

        action = self.path.rstrip("/").rsplit("/", 1)[-1]
        length = _to_int_or(self.headers.get("Content-Length") or 0, 0)
        raw = self.rfile.read(length) if length > 0 else b""
        try:
            body = json.loads(raw.decode("utf-8")) if raw else {}
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._send_json(
                200,
                {"status": "failed", "retcode": 1400, "message": "请求体不是合法 JSON", "data": None},
            )
            return
        if not isinstance(body, dict):
            body = {"raw": body}

        status, resp = self.echo.handle(action, body)
        self._send_json(status, resp)

    # ---- 工具 ----
    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt: str, *args: Any) -> None:  # 保留默认访问日志
        sys.stderr.write("[echo_server] %s - %s\n" % (self.address_string(), fmt % args))


def make_server(
    port: int = DEFAULT_PORT,
    *,
    host: str = "127.0.0.1",
    log_path: Optional[Path | str] = None,
    self_id: str = DEFAULT_SELF_ID,
    access_token: str = "",
) -> ThreadingHTTPServer:
    """构造（不启动）假 NapCat 服务。"""
    if log_path is None:
        # 默认写到项目 data/outbox/echo_server.jsonl
        root = Path(__file__).resolve().parents[3]
        log_path = root / "data" / "outbox" / "echo_server.jsonl"

    echo = EchoServer(log_path, self_id=self_id, access_token=access_token)

    class _Handler(EchoServerHandler):
        pass

    _Handler.echo = echo  # type: ignore[attr-defined]
    httpd = ThreadingHTTPServer((host, port), _Handler)
    httpd.echo = echo  # type: ignore[attr-defined]
    return httpd


def main(port: int = DEFAULT_PORT, *, host: str = "127.0.0.1", access_token: str = "") -> int:
    """启动服务并阻塞，直到 Ctrl+C。返回进程退出码。"""
    httpd = make_server(port, host=host, access_token=access_token)
    echo: EchoServer = httpd.echo  # type: ignore[attr-defined]
    print(f"[echo_server] 假 NapCat 已启动：http://{host}:{port}")
    print(f"[echo_server] 落盘文件：{echo.log_path}")
    print("[echo_server] 可用接口：/send_private_msg /send_group_msg /get_login_info")
    print("[echo_server] Ctrl+C 退出。")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[echo_server] 收到 Ctrl+C，正在关闭……")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    p = DEFAULT_PORT
    if len(sys.argv) > 1:
        try:
            p = int(sys.argv[1])
        except ValueError:
            print(f"端口必须是数字，收到：{sys.argv[1]!r}", file=sys.stderr)
            raise SystemExit(2)
    raise SystemExit(main(p))
