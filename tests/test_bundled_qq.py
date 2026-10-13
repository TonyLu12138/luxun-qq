"""Bundle tests use tiny inert files and mocked processes; never execute QQ."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from luxun_qq.desktop.bundled_qq import (
    inspect_bundle, launch_bundle, login_url, prepare_bundle, safe_member, sha256,
)


class BundleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.entries = {"qq_exe": "QQ/QQ.exe", "napcat_boot": "NapCat/NapCatWinBootMain.exe",
                        "napcat_hook": "NapCat/NapCatWinBootHook.dll", "napcat_main": "NapCat/napcat.mjs",
                        "napcat_package": "NapCat/qqnt.json"}
        self.archive = self.source / "qq-napcat.zip"
        with zipfile.ZipFile(self.archive, "w") as archive:
            for name in self.entries.values():
                archive.writestr(name, b"inert test file; never execute")
        self.manifest = {"format": 1, "components": ["qq", "napcat"], "archive": self.archive.name,
                         "sha256": sha256(self.archive), "qq_version": "test-qq", "napcat_version": "test-napcat",
                         "unpacked_bytes": 1000, **self.entries}
        self.write_manifest()

    def write_manifest(self):
        (self.source / "manifest.json").write_text(json.dumps(self.manifest), encoding="utf-8")

    def test_inspection_does_not_extract_or_run_components(self):
        with patch("subprocess.Popen", side_effect=AssertionError("must not execute")):
            report = inspect_bundle(self.source, verify=True)
        self.assertEqual(report["components"], ["qq", "napcat"])
        self.assertFalse(report["model_included"])
        self.assertFalse(report["engine_included"])
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["source"])

    def test_models_are_rejected(self):
        with zipfile.ZipFile(self.archive, "a") as archive:
            archive.writestr("models/unwanted.gguf", b"not a model")
        with self.assertRaisesRegex(ValueError, "模型权重"):
            inspect_bundle(self.source)

    def test_bad_digest_prevents_extraction(self):
        self.manifest["sha256"] = "0" * 64
        self.write_manifest()
        with self.assertRaisesRegex(ValueError, "校验失败"):
            prepare_bundle(self.source, self.root / "data")
        self.assertEqual(list((self.root / "data").iterdir()), [])

    def test_paths_cannot_escape_or_use_windows_streams(self):
        for name in ("../escape", "/absolute", "C:/absolute", "foo:stream", "folder/../escape", "folder./file"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                safe_member(name)

    def test_archive_traversal_is_rejected_before_extraction(self):
        with zipfile.ZipFile(self.archive, "a") as archive:
            archive.writestr("../escape.txt", b"unsafe")
        self.manifest["sha256"] = sha256(self.archive)
        self.write_manifest()
        with self.assertRaisesRegex(ValueError, "不安全路径"):
            prepare_bundle(self.source, self.root / "data")
        self.assertFalse((self.root / "escape.txt").exists())

    def test_preparation_never_runs_installer_and_reuses_login_data(self):
        with patch("subprocess.Popen", side_effect=AssertionError("must not execute")):
            first = prepare_bundle(self.source, self.root / "data")
            directory = Path(first["qq_bundle_dir"])
            profile = directory / "QQ" / "test-login-profile.txt"
            profile.write_text("preserve me")
            second = prepare_bundle(self.source, self.root / "data")
        self.assertEqual(first, second)
        self.assertEqual(profile.read_text(), "preserve me")
        self.assertEqual(len(list((self.root / "data").iterdir())), 1)

    def test_defaults_use_loopback_unique_tokens_and_matching_websocket(self):
        settings = prepare_bundle(self.source, self.root / "data")
        directory = Path(settings["qq_bundle_dir"])
        config = json.loads((directory / "NapCat/config/onebot11.json").read_text())
        server = config["network"]["websocketServers"][0]
        self.assertEqual(server["host"], "127.0.0.1")
        self.assertEqual(server["token"], settings["ws_token"])
        self.assertFalse(server["reportSelfMessage"])
        self.assertGreater(len(server["token"]), 30)
        webui = json.loads((directory / "NapCat/config/webui.json").read_text())
        self.assertEqual(webui["host"], "127.0.0.1")
        self.assertNotEqual(webui["token"], server["token"])
        self.assertIn(webui["token"], login_url(directory))

    def test_launch_uses_only_selected_bootloader_and_private_environment(self):
        settings = prepare_bundle(self.source, self.root / "data")
        directory = Path(settings["qq_bundle_dir"])
        with patch.dict("os.environ", {"NAPCAT_WORKDIR": "unrelated-directory"}), \
                patch("luxun_qq.desktop.bundled_qq.subprocess.Popen") as popen:
            launch_bundle(directory)
        command = popen.call_args.args[0]
        self.assertEqual(Path(command[0]).name, "NapCatWinBootMain.exe")
        self.assertEqual(Path(command[1]).name, "QQ.exe")
        self.assertEqual(Path(command[2]).name, "NapCatWinBootHook.dll")
        self.assertEqual(len(command), 3)
        self.assertNotIn("shell", popen.call_args.kwargs)
        self.assertIn("NAPCAT_LOAD_PATH", popen.call_args.kwargs["env"])
        self.assertEqual(popen.call_args.kwargs["env"]["NAPCAT_WORKDIR"], str((directory / "NapCat").resolve()))
        self.assertIn("file:///", (directory / "NapCat/loadNapCat.js").read_text())


if __name__ == "__main__":
    unittest.main()
