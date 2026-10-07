# -*- coding: utf-8 -*-
"""clipboard 适配器：把文本复制到 Windows 剪贴板，人工粘贴发送。

适用于「不想给程序发消息权限」的保守用法。优先用 ``tkinter``（无需额外程序），
失败则回退到 ``clip.exe`` 管道（Windows 自带）。无论成败都会写 outbox 记录。
"""
from __future__ import annotations

import shutil
import subprocess
import time
from typing import Any, Optional

from ..config import SenderConfig
from .base import SendResult, Sender

__all__ = ["ClipboardSender", "copy_to_clipboard"]


class ClipboardSender(Sender):
    """复制到剪贴板 + 落盘审计，不真正发送消息。"""

    name = "clipboard"

    def __init__(self, cfg: SenderConfig, outbox_dir) -> None:
        super().__init__(outbox_dir, timeout=cfg.timeout)
        self.cfg = cfg

    def send_text(self, peer_id: str, text: str) -> SendResult:
        ok, method, detail = copy_to_clipboard(text)
        record: dict[str, Any] = {
            "adapter": self.name,
            "action": "clipboard_copy",
            "peer_id": str(peer_id),
            "text": text,
            "copied": ok,
            "method": method,
            "ts": int(time.time()),
            "note": "clipboard：已复制，需要人工粘贴发送" if ok else "clipboard：复制失败",
        }
        path = self.record(
            "clipboard_copy",
            peer_id,
            text=text,
            ok=ok,
            detail=detail,
            raw=record,
        )
        if ok:
            print(f"[clipboard] 已复制（{method}），请手动粘贴发给 {peer_id}：{text}")
            return SendResult.success(
                self.name,
                f"已复制到剪贴板（{method}）。记录：{path}",
                record,
            )
        # 剪贴板不可用不是致命错误，要如实返回失败但保持流程可继续
        print(f"[clipboard] 复制失败：{detail}（内容已落盘：{path}）")
        return SendResult.failure(
            self.name,
            f"复制到剪贴板失败：{detail}（内容已落盘：{path}）",
            record,
        )


def copy_to_clipboard(text: str) -> tuple[bool, str, str]:
    """把文本写进 Windows 剪贴板。

    返回 ``(是否成功, 使用的方法, 中文说明)``。
    """
    ok, detail = _copy_with_tkinter(text)
    if ok:
        return True, "tkinter", "已通过 tkinter 写入剪贴板。"
    tk_detail = detail

    ok2, detail2 = _copy_with_clip(text)
    if ok2:
        return True, "clip.exe", "已通过 clip.exe 写入剪贴板。"

    return False, "", f"tkinter 失败（{tk_detail}）；clip.exe 失败（{detail2}）"


def _copy_with_tkinter(text: str) -> tuple[bool, str]:
    """优先方案：tkinter 剪贴板。无 GUI / 无 tkinter 时返回失败。"""
    try:
        import tkinter  # 标准库，但部分精简发行版会被裁掉
    except Exception as exc:  # pragma: no cover - 依赖发行版
        return False, f"无法导入 tkinter：{exc}"

    root: Optional[Any] = None
    try:
        root = tkinter.Tk()
        root.withdraw()                     # 不要弹出空窗口
        root.update_idletasks()
        root.clipboard_clear()
        root.clipboard_append(text)
        root.update()                       # 触发真正写入，否则退出后内容会丢
        # 读回校验，确认内容确实进去了
        got = root.clipboard_get()
        if str(got) != text:
            return False, "写入后读回内容不一致"
        return True, ""
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"
    finally:
        if root is not None:
            try:
                root.destroy()
            except Exception:  # pragma: no cover
                pass


def _copy_with_clip(text: str) -> tuple[bool, str]:
    """回退方案：``clip.exe`` 管道。"""
    exe = shutil.which("clip") or shutil.which("clip.exe")
    if not exe:
        return False, "找不到 clip.exe"
    try:
        proc = subprocess.run(
            [exe],
            input=text.encode("utf-16-le"),   # clip.exe 按 UTF-16LE 读取管道
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"{type(exc).__name__}: {exc}"
    if proc.returncode != 0:
        err = (proc.stderr or b"").decode("utf-8", errors="replace").strip()
        return False, f"clip.exe 退出码 {proc.returncode}：{err[:200]}"
    return True, ""
