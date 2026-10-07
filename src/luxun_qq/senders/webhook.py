# -*- coding: utf-8 -*-
"""webhook 适配器：把消息 POST 到你自己的中转脚本。

适合「不想直接暴露 NapCat HTTP 端口」或「要通过自建服务转发到多端」的场景。
请求体：``{"peer_id": str, "text": str, "timestamp": int, ...}``。
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any, Optional

from ..config import SenderConfig
from .base import SendError, SendResult, Sender

__all__ = ["WebhookSender"]


class WebhookSender(Sender):
    """把消息 POST 到 ``base_url``。"""

    name = "webhook"

    def __init__(
        self,
        cfg: SenderConfig,
        outbox_dir,
        *,
        base_url: Optional[str] = None,
        access_token: Optional[str] = None,
    ) -> None:
        super().__init__(outbox_dir, timeout=cfg.timeout)
        self.cfg = cfg
        self.base_url = (base_url or cfg.base_url or "").strip()
        if not self.base_url:
            raise SendError("WebhookSender 需要 SenderConfig.base_url（中转脚本地址），当前为空。")
        self.access_token = access_token if access_token is not None else (cfg.access_token or "")

    def send_text(self, peer_id: str, text: str) -> SendResult:
        ts = int(time.time())
        payload: dict[str, Any] = {
            "peer_id": str(peer_id),
            "text": text,
            "timestamp": ts,
            "adapter": self.name,
        }
        if self.cfg.self_id:
            payload["self_id"] = str(self.cfg.self_id)

        headers = {"Content-Type": "application/json; charset=utf-8"}
        if self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"

        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(self.base_url, data=body, method="POST", headers=headers)

        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                status = int(getattr(resp, "status", 0) or 0)
                raw_text = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            detail = _safe_read(exc)
            raise SendError(
                f"webhook 中转返回 HTTP {exc.code}：{detail[:300]}（地址 {self.base_url}）。"
                f"请检查中转脚本是否正常。"
            ) from exc
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            raise SendError(
                f"连接 webhook 中转失败（{self.base_url}）：{reason}。"
                f"请确认中转脚本已启动、地址可访问（局域网地址要写全 http://IP:PORT/path）。"
            ) from exc
        except TimeoutError as exc:
            raise SendError(
                f"webhook 请求超时（{self.timeout}s，地址 {self.base_url}）。"
            ) from exc

        record = {
            "adapter": self.name,
            "action": "webhook_post",
            "peer_id": str(peer_id),
            "text": text,
            "url": self.base_url,
            "http_status": status,
            "response": raw_text[:1000],
        }
        path = self.record(
            "webhook_post",
            peer_id,
            text=text,
            ok=True,
            detail=f"HTTP {status}",
            raw=record,
        )
        return SendResult.success(
            self.name,
            f"已 POST 到 {self.base_url}（HTTP {status}）。记录：{path}",
            record,
        )


def _safe_read(exc: urllib.error.HTTPError) -> str:
    try:
        body = exc.read()
    except Exception:  # pragma: no cover
        return ""
    return body.decode("utf-8", errors="replace") if body else ""
