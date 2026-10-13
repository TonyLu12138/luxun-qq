"""Desktop behavior using fake OneBot transport; no QQ, NapCat or model server."""
from __future__ import annotations

import json
import os
import queue
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from luxun_qq.desktop.controller import DesktopController
from luxun_qq.desktop.runtime import LocalModelProcess, import_components
from luxun_qq.desktop.settings import Settings, SettingsStore
from luxun_qq.desktop.transport import OneBotConnection
from luxun_qq.llm import EchoClient
from luxun_qq.senders.base import SendError


class FakeConnection:
    latest = None

    def __init__(self, url, token, on_event, on_disconnect):
        self.on_event = on_event
        self.on_disconnect = on_disconnect
        self.calls = []
        self.closed = False
        self.fail_send = False
        FakeConnection.latest = self

    def connect(self):
        pass

    def call(self, action, params, timeout=20):
        self.calls.append((action, params))
        if self.closed:
            raise SendError("closed")
        if action == "get_login_info":
            return {"status": "ok", "retcode": 0, "data": {"user_id": 99999}}
        return {"status": "failed" if self.fail_send else "ok",
                "retcode": 1 if self.fail_send else 0, "data": {"message_id": 9}}

    def close(self):
        self.closed = True

    def message(self, peer="10001", text="今天上班好累", message_id=None):
        self.on_event({"post_type": "message", "message_type": "private",
                       "user_id": peer, "message": text,
                       "message_id": message_id or str(time.monotonic_ns())})

    def sent(self):
        return [params for action, params in self.calls if action == "send_private_msg"]


class DesktopTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        self.controller = DesktopController(self.directory, connection_factory=FakeConnection)
        self.received = []
        self.engine_patch = patch("luxun_qq.engine.build_client", side_effect=lambda cfg: EchoClient(cfg))
        self.engine_patch.start()
        self.addCleanup(self.engine_patch.stop)

    def tearDown(self):
        self.controller.stop()
        if self.controller.worker:
            self.controller.worker.join(5)
            self.assertFalse(self.controller.running)
        self.temporary.cleanup()

    def event(self, kind, predicate=lambda event: True, timeout=4):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                event = self.controller.events.get(timeout=0.05)
            except queue.Empty:
                continue
            self.received.append(event)
            if event["kind"] == kind and predicate(event):
                return event
        self.fail(f"Timed out waiting for {kind}: {self.received[-5:]}")

    def start_live(self, **overrides):
        settings = Settings(whitelist="10001,10002", delay_min=0, delay_max=0, two_stage=False)
        for name, value in overrides.items():
            setattr(settings, name, value)
        self.controller.start(settings)
        self.event("status", lambda e: e["state"] == "running")
        return FakeConnection.latest

    def pending(self):
        return self.event("pending", lambda e: bool(e["items"]))["items"]

    def test_candidates_stay_in_app_and_only_edited_final_is_sent(self):
        conn = self.start_live()
        conn.message()
        item = self.pending()[0]
        self.assertEqual(len(item["candidates"]), 3)
        self.assertEqual(conn.sent(), [])
        self.controller.submit("send", peer=item["peer"], revision=item["revision"], text="我修改的回复")
        self.event("sent")
        self.assertEqual(conn.sent(), [{"user_id": 10001, "message": "我修改的回复"}])
        self.event("pending", lambda e: not e["items"])

    def test_qq_owner_commands_and_non_whitelisted_messages_are_ignored(self):
        conn = self.start_live()
        conn.message(peer="99999", text="a")
        self.event("pending", lambda e: not e["items"])
        conn.message(peer="12345")
        self.event("pending", lambda e: not e["items"])
        self.assertEqual(conn.sent(), [])
        self.assertEqual(self.controller.service.auto_peers, set())

    def test_automatic_sends_only_first_reply_without_control_notifications(self):
        conn = self.start_live(automatic=True)
        conn.message()
        sent = self.event("sent")
        self.event("pending", lambda e: not e["items"])
        self.assertEqual(conn.sent(), [{"user_id": 10001, "message": sent["text"]}])

    def test_failed_automatic_send_remains_pending(self):
        conn = self.start_live(automatic=True)
        conn.fail_send = True
        conn.message()
        items = self.pending()
        self.assertEqual(items[0]["peer"], "10001")
        self.assertTrue(any(e["kind"] == "error" for e in self.received))

    def test_stale_selection_cannot_send_new_message_candidate(self):
        conn = self.start_live()
        conn.message(text="第一条消息")
        old = self.pending()[0]
        conn.message(text="第二条消息")
        new = self.pending()[0]
        self.assertNotEqual(old["revision"], new["revision"])
        self.controller.submit("send", peer=old["peer"], revision=old["revision"], text="过时回复")
        self.event("error", lambda e: "已更新" in e["message"])
        self.assertEqual(conn.sent(), [])

    def test_multiple_peers_do_not_cross_send(self):
        conn = self.start_live()
        conn.message(peer="10001")
        first = self.pending()[0]
        conn.message(peer="10002")
        self.pending()
        self.controller.submit("send", peer=first["peer"], revision=first["revision"], text="只发给第一位")
        self.event("sent")
        remaining = self.pending()
        self.assertEqual([item["peer"] for item in remaining], ["10002"])
        self.assertEqual(conn.sent()[0]["user_id"], 10001)

    def test_regenerate_and_discard_do_not_send(self):
        conn = self.start_live()
        conn.message()
        item = self.pending()[0]
        self.controller.submit("automatic", enabled=True)
        self.event("notice", lambda e: "开启" in e["message"])
        self.event("pending")
        self.controller.submit("regenerate", peer=item["peer"], revision=item["revision"])
        new = self.pending()[0]
        self.assertNotEqual(new["revision"], item["revision"])
        self.assertEqual(conn.sent(), [])
        self.controller.submit("discard", peer=new["peer"], revision=new["revision"])
        self.event("pending", lambda e: not e["items"])
        self.assertEqual(conn.sent(), [])

    def test_pause_cancels_send_delay(self):
        conn = self.start_live(delay_min=2, delay_max=2)
        conn.message()
        item = self.pending()[0]
        self.controller.submit("send", peer=item["peer"], revision=item["revision"], text="应被取消")
        self.event("log", lambda e: "等待 2.0 秒" in e["message"])
        self.controller.set_paused(True)
        self.pending()
        self.assertEqual(conn.sent(), [])

    def test_stop_during_generation_prevents_automatic_send(self):
        entered = threading.Event()
        release = threading.Event()
        original = EchoClient.chat
        def delayed(client, *args, **kwargs):
            entered.set()
            release.wait(3)
            return original(client, *args, **kwargs)
        with patch.object(EchoClient, "chat", delayed):
            conn = self.start_live(automatic=True)
            conn.message()
            self.assertTrue(entered.wait(2))
            self.controller.stop()
            release.set()
            self.controller.worker.join(4)
        self.assertEqual(conn.sent(), [])
        self.assertFalse(self.controller.running)

    def test_duplicate_event_does_not_regenerate(self):
        conn = self.start_live()
        conn.message(message_id="same")
        first = self.pending()[0]
        conn.message(message_id="same")
        second = self.pending()[0]
        self.assertEqual(first["revision"], second["revision"])

    def test_demo_does_not_connect_or_share_live_memory(self):
        with patch.object(FakeConnection, "connect", side_effect=AssertionError("network forbidden")):
            self.controller.start(Settings(), demo=True)
            self.event("status", lambda e: e["state"] == "running")
            self.controller.submit("simulate", peer="10001", text="你好")
            item = self.pending()[0]
            self.controller.submit("send", peer=item["peer"], revision=item["revision"], text="演示回复")
            self.event("sent")
            self.assertTrue((self.directory / "demo" / "memory.db").exists())
            self.assertFalse((self.directory / "live").exists())

    def test_api_credentials_are_redacted_from_events(self):
        self.controller.secrets = ["secret-api-key", "secret-token"]
        self.controller.emit("error", message="key=secret-api-key, token=secret-token")
        error = self.event("error")
        self.assertNotIn("secret", error["message"])


class SettingsTests(unittest.TestCase):
    def test_backend_mapping_and_desktop_paths(self):
        settings = Settings(whitelist="10001", api_key="abc")
        cfg = settings.config(Path("C:/user-data"))
        self.assertEqual(cfg.llm.api_key, "abc")
        self.assertEqual(cfg.bot.owner_sink, "")
        self.assertEqual(cfg.bot.whitelist_file, "")
        settings.mode = "local_service"
        self.assertEqual(settings.config(Path("C:/user-data")).llm.backend, "local_ollama")
        settings.local_protocol = "openai"
        self.assertEqual(settings.config(Path("C:/user-data")).llm.backend, "openai_compat")

    def test_bad_live_settings_are_rejected_before_connect(self):
        with self.assertRaisesRegex(ValueError, "白名单"):
            Settings().validate()
        with self.assertRaisesRegex(ValueError, "延迟"):
            Settings(delay_min=30, delay_max=10).validate(demo=True)
        with self.assertRaisesRegex(ValueError, "地址"):
            Settings(whitelist="10001", api_url="file:///test").validate()
        with self.assertRaisesRegex(ValueError, "QQ 号"):
            Settings(whitelist="not-a-number").validate()

    @unittest.skipUnless(os.name == "nt", "Windows DPAPI")
    def test_secrets_are_encrypted_and_roundtrip(self):
        with tempfile.TemporaryDirectory() as temp:
            store = SettingsStore(Path(temp))
            original = Settings(api_key="sk-private-test", ws_token="napcat-private-test")
            store.save(original)
            encoded = store.path.read_text(encoding="utf-8")
            self.assertNotIn(original.api_key, encoded)
            self.assertNotIn(original.ws_token, encoded)
            self.assertEqual(store.load(), original)

    def test_component_import_cannot_recurse_into_itself(self):
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(ValueError, "子目录"):
                import_components(Path(temp), Path(temp) / "nested")

    def test_component_import_preserves_source_and_detects_paths(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "source"
            (source / "llama").mkdir(parents=True)
            (source / "models").mkdir()
            (source / "qq").mkdir()
            (source / "llama" / "llama-server.exe").write_bytes(b"fake, never execute")
            (source / "models" / "model.gguf").write_bytes(b"fake, never load")
            (source / "qq" / "launcher.bat").write_text("REM fake, never run")
            result = import_components(source, Path(temp) / "destination")
            self.assertTrue(Path(result["llama_exe"]).is_file())
            self.assertTrue(Path(result["gguf_path"]).is_file())
            self.assertTrue(Path(result["qq_launcher"]).is_file())
            self.assertTrue((source / "models" / "model.gguf").exists())


class TransportTests(unittest.TestCase):
    def test_response_echo_is_matched_while_events_remain_separate(self):
        events = []
        conn = OneBotConnection("ws://unused", "", events.append, lambda _: None)
        class Socket:
            def send(self, raw):
                request = json.loads(raw)
                conn._route({"post_type": "message", "message": "hello"})
                conn._route({"echo": "unrelated", "data": "wrong"})
                conn._route({"echo": request["echo"], "status": "ok", "data": "right"})
        conn.socket = Socket()
        response = conn.call("get_login_info", {}, timeout=0.1)
        self.assertEqual(response["data"], "right")
        self.assertEqual(len(events), 1)
        self.assertEqual(conn.waiters, {})

    def test_close_releases_waiting_call(self):
        conn = OneBotConnection("ws://unused", "", lambda _: None, lambda _: None)
        sent = threading.Event()
        class Socket:
            def send(self, raw):
                sent.set()
            def shutdown(self):
                pass
        conn.socket = Socket()
        errors = []
        def request():
            try:
                conn.call("send_private_msg", {}, timeout=5)
            except SendError as exc:
                errors.append(str(exc))
        thread = threading.Thread(target=request)
        thread.start()
        self.assertTrue(sent.wait(1))
        conn.close()
        thread.join(1)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(errors), 1)

    def test_timeout_does_not_retry_send(self):
        conn = OneBotConnection("ws://unused", "", lambda _: None, lambda _: None)
        requests = []
        class Socket:
            def send(self, raw):
                requests.append(raw)
        conn.socket = Socket()
        with self.assertRaisesRegex(SendError, "结果未知"):
            conn.call("send_private_msg", {}, timeout=0.01)
        self.assertEqual(len(requests), 1)


class LocalRuntimeTests(unittest.TestCase):
    def test_cancel_before_start_never_launches_a_component(self):
        cancelled = threading.Event()
        cancelled.set()
        with patch("luxun_qq.desktop.runtime.subprocess.Popen") as popen:
            with self.assertRaisesRegex(RuntimeError, "取消"):
                LocalModelProcess().start(Settings(), Path("unused"), cancelled, lambda _: None)
            popen.assert_not_called()

    def test_managed_local_runtime_uses_selected_model_and_stops_its_process(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            settings = Settings(mode="local_file", llama_exe=str(root / "llama-server.exe"),
                                gguf_path=str(root / "model.gguf"), gpu_layers=12)
            process = MagicMock()
            process.poll.return_value = None
            socket = MagicMock()
            socket.__enter__.return_value.getsockname.return_value = ("127.0.0.1", 12345)
            opener = MagicMock()
            opener.open.return_value.__enter__.return_value.status = 200
            with patch("luxun_qq.desktop.runtime.socket.socket", return_value=socket), \
                 patch("luxun_qq.desktop.runtime.subprocess.Popen", return_value=process) as popen, \
                 patch("luxun_qq.desktop.runtime.urllib.request.build_opener", return_value=opener):
                runtime = LocalModelProcess()
                endpoint = runtime.start(settings, root / "logs", threading.Event(), lambda _: None)
                runtime.stop()
                self.assertEqual(endpoint, "http://127.0.0.1:12345")
                command = popen.call_args.args[0]
                self.assertEqual(command[command.index("-m") + 1], str(Path(settings.gguf_path).resolve()))
                self.assertEqual(command[command.index("-ngl") + 1], "12")
                self.assertEqual(command[command.index("--host") + 1], "127.0.0.1")
                self.assertEqual(popen.call_args.kwargs.get("shell", False), False)
                process.terminate.assert_called_once()
                self.assertIsNone(runtime.log_file)

    def test_local_runtime_exit_reports_error_and_closes_log(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            process = MagicMock()
            process.poll.return_value = 1
            socket = MagicMock()
            socket.__enter__.return_value.getsockname.return_value = ("127.0.0.1", 12345)
            with patch("luxun_qq.desktop.runtime.socket.socket", return_value=socket), \
                 patch("luxun_qq.desktop.runtime.subprocess.Popen", return_value=process):
                runtime = LocalModelProcess()
                with self.assertRaisesRegex(RuntimeError, "已退出"):
                    runtime.start(Settings(llama_exe=str(root / "llama-server.exe")), root,
                                  threading.Event(), lambda _: None)
                self.assertIsNone(runtime.log_file)


if __name__ == "__main__":
    unittest.main()
