"""Desktop settings, writable data locations and Windows-protected credentials."""
from __future__ import annotations

import base64
import ctypes
import json
import os
import re
import sys
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from urllib.parse import urlparse

from ..config import Config


def app_directory() -> Path:
    override = os.environ.get("LUXUN_DESKTOP_HOME")
    if override:
        return Path(override).resolve()
    return Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "LuxunQQ"


def resource_directory() -> Path:
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[3]))


def _dpapi(data: bytes, *, decrypt: bool = False) -> bytes:
    if os.name != "nt":
        raise RuntimeError("保存密钥需要 Windows；其他系统的密钥仅在本次运行中使用。")
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_byte))]

    buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)))
    output = Blob()
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    function = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p,
                         ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                         wintypes.DWORD, ctypes.POINTER(Blob)]
    function.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    if not function(ctypes.byref(source), None, None, None, None, 1,
                    ctypes.byref(output)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(output.data, output.size)
    finally:
        kernel.LocalFree(output.data)


@dataclass
class Settings:
    mode: str = "api"
    api_url: str = "https://api.deepseek.com"
    api_model: str = "deepseek-chat"
    api_key: str = ""
    local_url: str = "http://127.0.0.1:11434"
    local_model: str = "qwen2.5:7b"
    local_protocol: str = "ollama"
    llama_exe: str = ""
    gguf_path: str = ""
    gpu_layers: int = 0
    context_size: int = 4096
    ws_url: str = "ws://127.0.0.1:3001"
    ws_token: str = ""
    qq_launcher: str = ""
    qq_bundle_dir: str = ""
    whitelist: str = ""
    target_all: bool = False
    automatic: bool = False
    persona: str = "emotion_master_male"
    delay_min: float = 10
    delay_max: float = 30
    memory: bool = True
    two_stage: bool = True

    def validate(self, *, demo: bool = False) -> None:
        if self.mode not in ("api", "local_service", "local_file"):
            raise ValueError("请选择 API、本地服务或本地 GGUF 模型。")
        if self.persona not in ("emotion_master_male", "luxun"):
            raise ValueError("请选择有效的回复角色。")
        if not (0 <= self.delay_min <= self.delay_max <= 300):
            raise ValueError("发送延迟需满足 0 ≤ 最小值 ≤ 最大值 ≤ 300 秒。")
        if demo:
            return
        self._url(self.ws_url, ("ws", "wss"), "QQ 连接地址")
        if not self.target_all and not self.peers():
            raise ValueError("请填写回复白名单，或明确勾选“回复所有私聊”。")
        if self.mode == "api":
            self._url(self.api_url, ("http", "https"), "API 地址")
            if not self.api_model.strip():
                raise ValueError("请填写 API 模型名称。")
        elif self.mode == "local_service":
            self._url(self.local_url, ("http", "https"), "本地服务地址")
            if self.local_protocol not in ("ollama", "openai"):
                raise ValueError("本地协议需选择 ollama 或 openai。")
            if not self.local_model.strip():
                raise ValueError("请填写本地模型名称。")
        else:
            if not Path(self.llama_exe).is_file():
                raise ValueError("请选择 llama-server.exe（需保留同目录 DLL）。")
            if not Path(self.gguf_path).is_file() or Path(self.gguf_path).suffix.lower() != ".gguf":
                raise ValueError("请选择有效的 GGUF 模型文件。")
            if not (512 <= self.context_size <= 131072) or self.gpu_layers < -1:
                raise ValueError("上下文需在 512～131072 之间；GPU 层数需 ≥ -1。")

    @staticmethod
    def _url(value: str, schemes: tuple[str, ...], label: str) -> None:
        parsed = urlparse(value)
        if parsed.scheme not in schemes or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError(f"{label}无效，请使用完整地址；凭据请填入专用字段。")
        try:
            parsed.port
        except ValueError as exc:
            raise ValueError(f"{label}端口无效。") from exc

    def peers(self) -> list[str]:
        result = re.split(r"[,，;；\s]+", self.whitelist.strip())
        result = [item for item in result if item]
        if any(not re.fullmatch(r"[0-9]{5,12}", item) for item in result):
            raise ValueError("白名单请填写 QQ 号，以空格、逗号或换行分隔。")
        return result

    def config(self, data_dir: Path, *, demo: bool = False,
               managed_url: str = "") -> Config:
        self.validate(demo=demo)
        cfg = Config()
        cfg.me = ""
        cfg.persona = self.persona
        cfg.bot.owner_sink = ""
        cfg.bot.owner = ""
        cfg.bot.whitelist_file = ""
        cfg.bot.target_mode = "all" if demo or self.target_all else "whitelist"
        cfg.bot.target_peer = "" if demo else ",".join(self.peers())
        cfg.bot.send_delay_min = 0 if demo else self.delay_min
        cfg.bot.send_delay_max = 0 if demo else self.delay_max
        cfg.bot.log_events = False
        cfg.bot.log_raw_body = False
        cfg.memory.db_path = str(data_dir / "memory.db")
        cfg.memory.enabled = self.memory
        # Desktop keeps facts/recent messages but avoids unmanaged background
        # extraction threads during shutdown. Existing facts remain usable.
        cfg.memory.extract_facts = False
        cfg.retrieve.enabled = False
        cfg.retrieve.style_sample = ""
        cfg.llm.two_stage = self.two_stage and not demo
        cfg.llm.voice_attempts = 1
        cfg.llm.timeout = 30
        if demo:
            cfg.llm.backend = "echo"
        elif self.mode == "api":
            cfg.llm.backend = "openai_compat"
            cfg.llm.base_url = self.api_url.strip()
            cfg.llm.model = self.api_model.strip()
            cfg.llm.api_key = self.api_key
        elif self.mode == "local_service":
            cfg.llm.backend = "local_ollama" if self.local_protocol == "ollama" else "openai_compat"
            cfg.llm.base_url = self.local_url.strip()
            cfg.llm.model = self.local_model.strip()
        else:
            cfg.llm.backend = "openai_compat"
            cfg.llm.base_url = managed_url
            cfg.llm.model = "desktop-local"
        return cfg


class SettingsStore:
    def __init__(self, directory: Path):
        self.directory = directory
        self.path = directory / "desktop.json"

    def load(self) -> Settings:
        if not self.path.exists():
            return Settings()
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        allowed = {f.name for f in fields(Settings)}
        values = {k: v for k, v in raw.items() if k in allowed and k not in ("api_key", "ws_token")}
        for key in ("api_key", "ws_token"):
            encrypted = raw.get(key + "_protected")
            if encrypted:
                values[key] = _dpapi(base64.b64decode(encrypted), decrypt=True).decode("utf-8")
        return Settings(**values)

    def save(self, settings: Settings) -> None:
        raw = asdict(settings)
        for key in ("api_key", "ws_token"):
            value = raw.pop(key)
            if value:
                raw[key + "_protected"] = base64.b64encode(_dpapi(value.encode("utf-8"))).decode("ascii")
        self.directory.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.path)
