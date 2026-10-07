# -*- coding: utf-8 -*-
"""事件镜像：接收 NapCat 上报，记录原始内容，并转发给真正的服务。

用途：诊断「服务收到了 POST 但不生成候选」——需要看清 NapCat 实际发了什么。

它做三件事：
  1. 监听 18899，接收 NapCat 的事件上报
  2. 把每个事件的类型与关键字段打印出来，并落盘到 _runtime/events.jsonl
  3. 原样转发给 http://127.0.0.1:8848/（真正的 luxun-qq 服务）

用法：
    python tools/event_mirror.py
然后把你 NapCat 的「HTTP 事件上报」地址临时改成 http://127.0.0.1:18899/
（或者直接在 WebUI 里把原 8848 改成 18899 / 新增一个上报地址）
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LISTEN_PORT = 18899
FORWARD_TO = "http://127.0.0.1:8848/"
LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                   "_runtime", "events.jsonl")


def summarise(ev: dict) -> str:
    """把事件压成一行摘要，便于肉眼判断类型。"""
    if not isinstance(ev, dict):
        return "<非 JSON>"
    post_type = ev.get("post_type", "?")
    if post_type == "message":
        msg = ev.get("message")
        if isinstance(msg, list):
            text = "".join(seg.get("data", {}).get("text", "")
                           for seg in msg if seg.get("type") == "text")
        else:
            text = str(msg)
        return (f"message/{ev.get('message_type')} "
                f"from={ev.get('user_id')} self={ev.get('self_id')} "
                f"text={text!r} msgid={ev.get('message_id')}")
    if post_type == "meta_event":
        return f"meta_event/{ev.get('meta_event_type')}（心跳，正常会被忽略）"
    if post_type == "notice":
        return f"notice/{ev.get('notice_type')}"
    if post_type == "request":
        return f"request/{ev.get('request_type')}"
    return f"{post_type} keys={list(ev.keys())[:8]}"


class Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b"{}"
        try:
            ev = json.loads(raw.decode("utf-8"))
        except Exception:
            ev = {"_raw": raw[:500].decode("utf-8", "ignore")}

        line = summarise(ev)
        print(f"[{time.strftime('%H:%M:%S')}] {line}", flush=True)
        try:
            os.makedirs(os.path.dirname(LOG), exist_ok=True)
            with open(LOG, "a", encoding="utf-8") as f:
                f.write(json.dumps(ev, ensure_ascii=False) + "\n")
        except Exception:
            pass

        # 转发给真正的服务
        status = "-"
        try:
            req = urllib.request.Request(
                FORWARD_TO, data=raw,
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=120) as r:
                status = r.status
        except urllib.error.HTTPError as e:
            status = f"HTTP {e.code}"
        except Exception as e:
            status = type(e).__name__

        body = json.dumps({"status": "ok", "forwarded": status}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        body = b'{"status":"ok","mirror":true}'
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a) -> None:
        pass


def main() -> int:
    srv = ThreadingHTTPServer(("127.0.0.1", LISTEN_PORT), Handler)
    srv.daemon_threads = True
    print("=" * 68)
    print("事件镜像已启动")
    print("=" * 68)
    print(f"  监听      : http://127.0.0.1:{LISTEN_PORT}/")
    print(f"  转发到    : {FORWARD_TO}")
    print(f"  原始事件  : {os.path.abspath(LOG)}")
    print()
    print("请把 NapCat 的「HTTP 事件上报」地址改成：")
    print(f"    http://127.0.0.1:{LISTEN_PORT}/")
    print("然后重新登录 / 让 1987654321 发一条消息。")
    print()
    print("这里会逐条打印事件类型，可直接看出消息事件有没有被上报。")
    print("Ctrl+C 停止。")
    print("=" * 68)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        srv.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
