# -*- coding: utf-8 -*-
"""OneBot WebSocket 服务：连接 NapCat 的 WS 服务器，收事件 + 发消息。

与 service.py（HTTP webhook 版）的分工：
    - service.py    ：NapCat 主动把事件 POST 给我们（需要配 HTTP 上报）
    - service_ws.py ：我们主动连 NapCat 的 WebSocket 服务器（只配 WS 服务端）

两者共用同一套三步思考引擎与审批状态机（BotService），只是传输层不同。
WS 版更省事：不用配 HTTP 事件上报，也不受其行为差异影响。
"""
from __future__ import annotations

import json
import time
from typing import Any, Optional

from .config import ROOT, Config
from .engine import ReplyEngine
from .senders.base import SendError
from .senders.ws import WsSender
from .service import OUTBOX_DIR, BotService, stop_model_if_configured
from .wsclient import WSClient, WSError

__all__ = ["run_ws", "build_ws_service"]


def build_ws_service(cfg: Config, client: WSClient) -> tuple[BotService, WsSender]:
    """组装 WS 版服务：BotService + WsSender 共用同一条连接。"""
    sender = WsSender(client, OUTBOX_DIR, self_id=cfg.sender.self_id,
                      timeout=cfg.sender.timeout)
    engine = ReplyEngine(cfg)
    svc = BotService(cfg, engine=engine, sender=sender)
    return svc, sender


def run_ws(cfg: Config, *, url: Optional[str] = None, token: Optional[str] = None,
           max_retries: int = 0) -> int:
    """连接 NapCat 的 WebSocket 服务器并持续处理事件。

    Args:
        url: ws 地址；默认按 sender.base_url 推导（http->ws、端口换成 ws 端口）
        token: 鉴权 token；默认取 cfg.sender.access_token
        max_retries: 断线重连次数上限，0 表示无限重连
    """
    ws_url = url or _derive_ws_url(cfg)
    ws_token = cfg.sender.access_token if token is None else token

    print("=" * 68)
    print("luxun-qq WebSocket 服务")
    print("=" * 68)
    print(f"  NapCat WS   : {ws_url}")
    print(f"  我的号      : {cfg.sender.self_id}")
    print(f"  回复对象    : {cfg.bot.target_peer}")
    print(f"  owner       : {cfg.bot.owner}")
    print(f"  候选投递到  : {cfg.bot.owner_sink}")
    print(f"  推理后端    : {cfg.llm.backend} / {cfg.llm.model}")
    print("=" * 68)

    attempts = 0
    try:
        _ws_loop(cfg, ws_url, ws_token, max_retries)
    except KeyboardInterrupt:
        print("\n收到中断，正在停止…")
    finally:
        # 退出时按配置释放显存（默认关闭；模型服务自己的窗口关掉也会释放）
        stop_model_if_configured(cfg)
    return 0


def _ws_loop(cfg: Config, ws_url: str, ws_token: str,
             max_retries: int) -> None:
    """连接并处理事件；断线自动重连，永不返回（除非触发上限）。"""
    attempts = 0
    while True:
        attempts += 1
        client = WSClient(ws_url, token=ws_token, timeout=120)
        try:
            client.connect()
        except Exception as e:
            print(f"[连接失败] {type(e).__name__}: {e}")
            if max_retries and attempts >= max_retries:
                return
            print("  5 秒后重试…（请确认 NapCat 已开启 WebSocket 服务器）")
            time.sleep(5)
            continue

        print(f"[已连接] {ws_url}")
        svc, sender = build_ws_service(cfg, client)
        try:
            info = sender.get_login_info()
            if info:
                print(f"[登录信息] {json.dumps(info, ensure_ascii=False)}")
        except Exception as e:
            print(f"[提示] 获取登录信息失败（不影响收消息）：{e}")

        idle_logged = False
        try:
            while True:
                # 先处理等待响应期间夹带收到的事件
                for ev in sender.take_deferred():
                    _dispatch(svc, ev)

                text = client.recv()
                if text is None:
                    print("[断开] 连接被对端关闭，准备重连…")
                    break
                if not idle_logged:
                    idle_logged = True
                try:
                    event = json.loads(text)
                except Exception:
                    print(f"[跳过] 非 JSON 帧：{text[:120]!r}")
                    continue
                _dispatch(svc, event)
        except (WSError, OSError) as e:
            print(f"[断开] {type(e).__name__}: {e}，准备重连…")
        finally:
            client.close()

        if max_retries and attempts >= max_retries:
            return
        time.sleep(3)


def _dispatch(svc: BotService, event: Any) -> None:
    """把一条事件交给 BotService，异常不外泄。"""
    if not isinstance(event, dict):
        return
    # API 响应帧（带 echo、无 post_type）不是事件，跳过
    if "post_type" not in event and "echo" in event:
        return
    try:
        svc.handle_event(event)
    except Exception as e:
        svc.log(f"[异常] 处理事件出错：{type(e).__name__}: {e}")


def _derive_ws_url(cfg: Config) -> str:
    """从配置推导 WebSocket 地址。

    优先用 cfg.bot.ws_url；否则按 sender.base_url 推导：
        http://127.0.0.1:3000  ->  ws://127.0.0.1:3001
    （NapCat 的 HTTP 服务与 WS 服务通常相邻端口，但更稳妥是显式配置。）
    """
    explicit = getattr(cfg.bot, "ws_url", "") or ""
    if explicit:
        return explicit

    base = (cfg.sender.base_url or "").strip()
    if not base:
        return "ws://127.0.0.1:3001"
    ws = base.replace("https://", "wss://").replace("http://", "ws://")
    # NapCat 默认 WS 服务端端口是 3001，与 HTTP 的 3000 区分
    return ws.rstrip("/")
