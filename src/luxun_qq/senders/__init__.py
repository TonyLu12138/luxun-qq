# -*- coding: utf-8 -*-
"""可插拔发送层。

按 ``SenderConfig.adapter`` 分发：

``dry_run``   默认，离线只落盘
``napcat``    OneBot 11 HTTP（真 NapCat）
``webhook``   POST 到你自己的中转脚本
``clipboard`` 复制到 Windows 剪贴板，人工粘贴

所有适配器都会把「到底发了什么」追加到 ``outbox_dir/*.jsonl``。
"""
from __future__ import annotations

from pathlib import Path

from ..config import SenderConfig
from .base import SendError, SendResult, Sender, append_outbox, outbox_path
from .clipboard import ClipboardSender, copy_to_clipboard
from .dry_run import DryRunSender
from .napcat import NapCatSender
from .webhook import WebhookSender

__all__ = [
    "SendResult",
    "SendError",
    "Sender",
    "DryRunSender",
    "NapCatSender",
    "WebhookSender",
    "ClipboardSender",
    "build_sender",
    "available_adapters",
    "append_outbox",
    "outbox_path",
    "copy_to_clipboard",
]

#: 可用适配器名（工厂分发依据）
ADAPTERS: tuple[str, ...] = ("dry_run", "napcat", "webhook", "clipboard")


def available_adapters() -> tuple[str, ...]:
    """返回可用适配器名，供 CLI 报错与文档使用。"""
    return ADAPTERS


def build_sender(cfg: SenderConfig, outbox_dir: Path | str) -> Sender:
    """按 ``cfg.adapter`` 构造发送器。

    ``outbox_dir`` 通常传 ``config.DATA_DIR / "outbox"``。
    未知适配器抛 ``SendError`` 并列出可用项。
    """
    adapter = (cfg.adapter or "dry_run").strip().lower()
    outbox = Path(outbox_dir)

    if adapter == "dry_run":
        return DryRunSender(outbox, timeout=cfg.timeout)
    if adapter == "napcat":
        return NapCatSender(cfg, outbox)
    if adapter == "webhook":
        return WebhookSender(cfg, outbox)
    if adapter == "clipboard":
        return ClipboardSender(cfg, outbox)

    raise SendError(
        f"未知的发送适配器：{cfg.adapter!r}。可用项：{' / '.join(ADAPTERS)}。"
        f"可在 config.yaml 的 sender.adapter 或用环境变量 LUXUN_SENDER_ADAPTER 指定。"
    )
