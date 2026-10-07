# -*- coding: utf-8 -*-
"""NapCat 适配器：OneBot 11 HTTP 协议。

对接 NapCat（或任何 OneBot 11 实现）的 HTTP 接口：

* 发私聊：``POST {base_url}/send_private_msg``，body ``{"user_id": int, "message": str}``；
* 登录信息：``POST {base_url}/get_login_info``；
* 发群聊：``POST {base_url}/send_group_msg``（附带能力）。

返回体约定 ``{"status": "ok", "retcode": 0, "data": {...}}``，``retcode != 0`` 视为失败。
只使用标准库 ``urllib.request``。
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Optional

from ..config import SenderConfig
from .base import SendError, SendResult, Sender

__all__ = ["NapCatSender"]


class NapCatSender(Sender):
    """OneBot 11 HTTP 发送适配器。"""

    name = "napcat"

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
        self.base_url = (base_url or cfg.base_url or "http://127.0.0.1:3000").rstrip("/")
        self.access_token = access_token if access_token is not None else (cfg.access_token or "")

    # ------------------------------------------------------------------ #
    # 对外能力
    # ------------------------------------------------------------------ #
    def send_text(self, peer_id: str, text: str) -> SendResult:
        """发私聊消息。"""
        user_id = _to_int(peer_id, "私聊 peer_id（QQ 号）")
        data = self._call("/send_private_msg", {"user_id": user_id, "message": text})
        record = {
            "adapter": self.name,
            "action": "send_private_msg",
            "peer_id": str(peer_id),
            "user_id": user_id,
            "message": text,
            "sent": True,
            "response": data,
        }
        path = self.record(
            "send_private_msg",
            peer_id,
            text=text,
            ok=True,
            detail=f"NapCat 已受理（message_id={data.get('message_id')}）",
            raw=record,
        )
        return SendResult.success(
            self.name,
            f"已发送给 {peer_id}（NapCat message_id={data.get('message_id')}）。记录：{path}",
            record,
        )

    def send_group_msg(self, group_id: str, text: str) -> SendResult:
        """发群聊消息（附带能力，不是主链路）。"""
        gid = _to_int(group_id, "群号 group_id")
        data = self._call("/send_group_msg", {"group_id": gid, "message": text})
        record = {
            "adapter": self.name,
            "action": "send_group_msg",
            "group_id": gid,
            "message": text,
            "sent": True,
            "response": data,
        }
        path = self.record(
            "send_group_msg",
            str(group_id),
            text=text,
            ok=True,
            detail=f"NapCat 群消息已受理（message_id={data.get('message_id')}）",
            raw=record,
        )
        return SendResult.success(
            self.name,
            f"已发送到群 {group_id}（message_id={data.get('message_id')}）。记录：{path}",
            record,
        )

    def get_login_info(self) -> dict[str, Any]:
        """查询当前登录账号信息，可用于验证 NapCat 是否在线。"""
        return self._call("/get_login_info", {})

    def send_candidates(
        self,
        peer_id: str,
        candidates: list[str],
        chosen: Optional[int] = None,
    ) -> SendResult:
        """沿用基类语义：``chosen=None`` 只展示（落盘），否则发出选中项。"""
        return super().send_candidates(peer_id, candidates, chosen)

    # ------------------------------------------------------------------ #
    # HTTP 细节
    # ------------------------------------------------------------------ #
    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json; charset=utf-8"}
        if self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"
        return headers

    def _call(self, endpoint: str, body: dict[str, Any]) -> dict[str, Any]:
        """调用一个 OneBot HTTP 接口，返回 ``data`` 字段（并校验 retcode）。"""
        url = f"{self.base_url}{endpoint}"
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=payload, method="POST", headers=self._headers())

        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw_bytes = resp.read()
        except urllib.error.HTTPError as exc:
            detail = _safe_read(exc)
            if exc.code in (401, 403):
                raise SendError(
                    f"NapCat 拒绝认证（HTTP {exc.code}，{url}）。"
                    f"请检查 SenderConfig.access_token / 环境变量 LUXUN_SENDER_TOKEN。"
                    f"服务响应：{detail[:200]}"
                ) from exc
            if exc.code == 404:
                raise SendError(
                    f"NapCat 没有这个接口：{endpoint}（HTTP 404，{url}）。"
                    f"请确认 NapCat 的 HTTP 服务已开启、base_url 是否正确（不要带 /v1）。"
                ) from exc
            raise SendError(f"NapCat 返回 HTTP {exc.code}：{detail[:300]}（地址 {url}）") from exc
        except urllib.error.URLError as exc:
            raise SendError(_connection_hint(exc, self.base_url)) from exc
        except TimeoutError as exc:
            raise SendError(
                f"NapCat 请求超时（{self.timeout}s，地址 {url}）。"
                f"可以调大 SenderConfig.timeout。"
            ) from exc

        text = raw_bytes.decode("utf-8", errors="replace")
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise SendError(
                f"NapCat 返回的不是合法 JSON：{text[:200]!r}（地址 {url}）。"
                f"请确认 base_url 指向的是 OneBot HTTP 服务而不是别的端口。"
            ) from exc

        if not isinstance(data, dict):
            raise SendError(f"NapCat 返回结构异常（期望 JSON 对象）：{text[:200]!r}")

        status = str(data.get("status") or "")
        retcode = data.get("retcode")
        if status not in ("ok", "async") or (retcode not in (0, None)):
            raise SendError(
                f"NapCat 调用 {endpoint} 失败：status={status!r} retcode={retcode!r} "
                f"message={data.get('message') or data.get('msg') or data.get('wording')!r}。"
                f"请检查 peer_id 是否存在、协议端是否在线。"
            )

        out = data.get("data")
        return out if isinstance(out, dict) else {"data": out}


def _to_int(value: Any, label: str) -> int:
    """OneBot 的 id 必须是整数，这里做一次带中文提示的转换。"""
    try:
        return int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise SendError(
            f"{label}必须是数字（OneBot 协议要求整数），当前为 {value!r}。"
        ) from exc


def _safe_read(exc: urllib.error.HTTPError) -> str:
    try:
        body = exc.read()
    except Exception:  # pragma: no cover
        return ""
    return body.decode("utf-8", errors="replace") if body else ""


def _connection_hint(exc: urllib.error.URLError, base_url: str) -> str:
    """连接类错误 -> 中文提示（重点照顾「服务没起来」这一常见情况）。"""
    reason = getattr(exc, "reason", exc)
    text = str(reason)
    low = text.lower()
    if "refused" in low or "10061" in text:
        return (
            f"连接被拒绝（{base_url}）：NapCat 未运行，或 HTTP 服务未开启。"
            f"请先启动 NapCat 并在配置里打开 OneBot 11 HTTP 服务（默认端口 3000），"
            f"network.websocketServer / httpServer 的地址要与 base_url 一致。"
        )
    if "timed out" in low or "timeout" in low:
        return f"连接超时（{base_url}）：NapCat 可能卡住或端口被占用，请检查服务状态。"
    if "getaddrinfo" in low or "11001" in text:
        return f"域名解析失败（{base_url}）：base_url 写错了，应形如 http://127.0.0.1:3000。"
    return (
        f"连接 NapCat 失败（{base_url}）：{text}。"
        f"请确认 NapCat 已启动、OneBot HTTP 服务已开启、base_url 与 access_token 正确。"
    )
