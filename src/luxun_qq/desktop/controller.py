"""Desktop worker: all reply state and SQLite access stay off the UI thread."""
from __future__ import annotations

import queue
import threading
import uuid
from pathlib import Path

from ..engine import ReplyEngine
from ..senders.base import SendError, SendResult, Sender
from ..service import BotService
from .runtime import LocalModelProcess
from .settings import Settings
from .transport import DesktopSender, OneBotConnection


class DemoSender(Sender):
    name = "desktop-demo"

    def send_text(self, peer_id: str, text: str) -> SendResult:
        self.record("demo_send", peer_id, text=text, ok=True)
        return SendResult.success(self.name, "仅模拟发送，未连接 QQ")


class DesktopService(BotService):
    def __init__(self, cfg, *, sender, emit, cancelled, paused):
        self.emit = emit
        self.cancelled = cancelled
        self.paused = paused
        super().__init__(cfg, sender=sender, engine=ReplyEngine(cfg),
                         log=lambda message: emit("log", message=message))

    def _notify_owner(self, text: str) -> bool:
        self.emit("notice", message=text)
        return True

    def _on_owner_command(self, text: str):
        # The desktop is the only control surface. QQ text can never act as a
        # send/auto-mode command, including messages from the logged-in account.
        return None

    def _send(self, peer_id: str, text: str, *, delay_reason: str = "") -> bool:
        delay = self._human_delay()
        if delay:
            self.emit("log", message=f"等待 {delay:.1f} 秒后发送给 {peer_id}…")
        # Pause/stop can cancel a delayed send without waiting for the model or
        # command queue. A request already handed to QQ cannot be recalled.
        remaining = delay
        while remaining > 0:
            if self.cancelled.is_set() or self.paused.is_set():
                return False
            chunk = min(remaining, 0.1)
            self.cancelled.wait(chunk)
            remaining -= chunk
        if self.cancelled.is_set() or self.paused.is_set():
            return False
        try:
            result = self.sender.send_text(peer_id, text)
        except Exception as exc:
            self.emit("error", message=f"发送失败：{exc}")
            return False
        if not result.ok:
            self.emit("error", message=f"发送失败：{result.detail}")
            return False
        self.remember(peer_id, mine=True, text=text)
        self.emit("sent", peer=peer_id, text=text)
        return True

    def _auto_reply(self, peer_id: str, text: str) -> dict:
        # Generate a visible pending item first, so failed/cancelled automatic
        # sends remain available for inspection instead of being silently lost.
        from ..service import Pending
        plan = self._plan_with_memory(peer_id, text)
        self.pending[peer_id] = Pending(peer_id=peer_id, plan=plan, incoming_text=text,
                                        labels=self.engine.candidate_labels())
        ok = self._send(peer_id, plan.candidates()[0].text)
        if ok:
            self.pending.pop(peer_id, None)
        return {"handled": True, "action": "auto_sent" if ok else "auto_send_failed"}


class DesktopController:
    def __init__(self, directory: Path, *, connection_factory=OneBotConnection):
        self.directory = directory
        self.connection_factory = connection_factory
        self.events: queue.Queue = queue.Queue()
        self.commands: queue.Queue = queue.Queue()
        self.cancelled = threading.Event()
        self.paused = threading.Event()
        self.worker = None
        self.connection = None
        self.service = None
        self.demo = False
        self.revisions: dict[str, tuple[object, str]] = {}
        self.secrets: list[str] = []

    @property
    def running(self) -> bool:
        return self.worker is not None and self.worker.is_alive()

    def emit(self, kind: str, **payload) -> None:
        # Provider exceptions can include URLs/headers. Keep credentials out of
        # every UI event, including logs and error dialogs.
        def clean(value):
            if isinstance(value, str):
                for secret in self.secrets:
                    value = value.replace(secret, "***")
            elif isinstance(value, dict):
                value = {k: clean(v) for k, v in value.items()}
            elif isinstance(value, list):
                value = [clean(v) for v in value]
            return value
        self.events.put({"kind": kind, **clean(payload)})

    def start(self, settings: Settings, *, demo: bool = False) -> None:
        if self.running:
            raise RuntimeError("请等待当前服务完全停止后再启动。")
        settings.validate(demo=demo)
        self.demo = demo
        self.commands = queue.Queue()
        self.revisions.clear()
        self.cancelled.clear()
        self.paused.clear()
        self.secrets = [s for s in (settings.api_key, settings.ws_token) if s]
        self.worker = threading.Thread(target=self._run, args=(settings,), daemon=True,
                                       name="desktop-worker")
        self.worker.start()

    def submit(self, kind: str, **payload) -> None:
        if not self.running or self.cancelled.is_set():
            raise RuntimeError("请先启动服务或离线演示。")
        self.commands.put({"kind": kind, **payload})

    def set_paused(self, value: bool) -> None:
        if value:
            self.paused.set()
        else:
            self.paused.clear()
        self.emit("status", state="paused" if value else "running",
                  message="已暂停接收与发送" if value else "服务已恢复")

    def stop(self) -> None:
        self.cancelled.set()
        if self.connection is not None:
            self.connection.close()
        self.emit("status", state="stopping", message="正在停止；等待在途生成结束…")

    def _incoming(self, event) -> None:
        if not self.cancelled.is_set() and not self.paused.is_set():
            self.commands.put({"kind": "message", "event": event})

    def _disconnected(self, message) -> None:
        self.emit("error", message=message)
        self.cancelled.set()

    def _snapshot(self) -> None:
        items = []
        pending = self.service.pending
        for peer in list(self.revisions):
            if peer not in pending:
                self.revisions.pop(peer)
        for peer, item in pending.items():
            prior = self.revisions.get(peer)
            if prior is None or prior[0] is not item:
                self.revisions[peer] = (item, uuid.uuid4().hex)
            items.append({"peer": peer, "revision": self.revisions[peer][1],
                          "incoming": item.incoming_text,
                          "candidates": [c.text for c in item.plan.candidates()[:3]],
                          "analysis": item.plan.render()})
        self.emit("pending", items=items)

    def _run(self, settings: Settings) -> None:
        self.emit("status", state="starting", message="正在启动…")
        local_model = LocalModelProcess()
        data_dir = self.directory / ("demo" if self.demo else "live")
        try:
            data_dir.mkdir(parents=True, exist_ok=True)
            endpoint = ""
            if settings.mode == "local_file" and not self.demo:
                endpoint = local_model.start(settings, data_dir, self.cancelled,
                                             lambda m: self.emit("log", message=m))
            if self.cancelled.is_set():
                return
            cfg = settings.config(data_dir, demo=self.demo, managed_url=endpoint)
            if self.demo:
                sender = DemoSender(data_dir / "outbox")
            else:
                self.connection = self.connection_factory(settings.ws_url, settings.ws_token,
                                                          self._incoming, self._disconnected)
                if self.cancelled.is_set():
                    return
                self.connection.connect()
                sender = DesktopSender(self.connection, data_dir / "outbox")
                response = self.connection.call("get_login_info", {}, 10)
                info = response.get("data") or {}
                if response.get("status") != "ok" or not info.get("user_id"):
                    raise SendError("无法确认 QQ 登录状态，请先在 QQ/NapCat 中登录。")
                cfg.bot.owner = str(info["user_id"])
                cfg.sender.self_id = cfg.bot.owner
                sender.self_id = cfg.bot.owner
                self.emit("log", message=f"QQ 已连接：{cfg.bot.owner}")
            self.service = DesktopService(cfg, sender=sender, emit=self.emit,
                                          cancelled=self.cancelled, paused=self.paused)
            if settings.automatic:
                self.service.auto_peers.add("*")
            if self.cancelled.is_set():
                return
            self.emit("status", state="running",
                      message="离线演示 · 不会发送 QQ 消息" if self.demo else "已连接 · 等待私聊消息")
            while not self.cancelled.is_set():
                try:
                    command = self.commands.get(timeout=0.15)
                except queue.Empty:
                    continue
                try:
                    self.emit("busy", value=True)
                    self._handle(command)
                except Exception as exc:
                    self.emit("error", message=str(exc))
                finally:
                    if not self.cancelled.is_set():
                        self._snapshot()
                    self.emit("busy", value=False)
        except Exception as exc:
            if not self.cancelled.is_set():
                self.emit("error", message=f"启动失败：{exc}")
        finally:
            if self.connection is not None:
                self.connection.close()
                self.connection = None
            try:
                local_model.stop()
                if self.service is not None and self.service.memory is not None:
                    self.service.memory.close()
            except Exception as exc:
                self.emit("error", message=f"资源清理失败：{exc}")
            finally:
                self.service = None
                self.emit("pending", items=[])
                self.emit("status", state="stopped", message="已停止")

    def _handle(self, command) -> None:
        kind = command["kind"]
        svc = self.service
        if kind == "automatic":
            svc.auto_peers = {"*"} if command["enabled"] else set()
            self.emit("notice", message="已开启自动托管（默认发送第一条候选）" if command["enabled"]
                      else "已切换为人工选择")
            return
        if self.paused.is_set():
            self.emit("notice", message="当前已暂停，此次操作或消息未处理。")
            return
        if kind == "simulate":
            if not self.demo:
                raise ValueError("模拟消息只能在离线演示中使用。")
            peer = command.get("peer", "10001")
            if not str(peer).isdigit() or not command["text"].strip():
                raise ValueError("请输入演示 QQ 号及消息内容。")
            svc.handle_event({"post_type": "message", "message_type": "private",
                              "user_id": peer, "message_id": uuid.uuid4().hex,
                              "message": command["text"]})
            return
        if kind == "message":
            event = command["event"]
            # Never interpret the account's own QQ messages as application commands.
            if str(event.get("user_id", "")) != svc.cfg.sender.self_id:
                svc.handle_event(event)
            return
        peer = command.get("peer")
        prior = self.revisions.get(peer)
        if prior is None or prior[1] != command.get("revision") or svc.pending.get(peer) is not prior[0]:
            raise ValueError("该候选已更新或已处理，请重新选择最新消息。")
        pending = svc.pending[peer]
        if kind == "send":
            text = command["text"].strip()
            if not text:
                raise ValueError("回复内容不能为空。")
            if svc._send(peer, text):
                svc.pending.pop(peer, None)
        elif kind == "discard":
            svc.pending.pop(peer, None)
        elif kind == "regenerate":
            # Regenerate never automatically sends, even if auto mode was toggled.
            automatic, svc.auto_peers = svc.auto_peers, set()
            try:
                svc._on_target_message(peer, pending.incoming_text, store=False)
            finally:
                svc.auto_peers = automatic
