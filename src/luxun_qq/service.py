# -*- coding: utf-8 -*-
"""OneBot 11 事件服务：接收 NapCat 推送，生成三候选，审批后自动发送。

完整链路：
    NapCat(1234567890) ──webhook──▶ 本服务
        │
        ├─ 收到 target_peer(1987654321) 的消息
        │     └─▶ 三步思考 + 三候选
        │           └─▶ 把候选推给 owner(1234567890) 的「文件传输助手」式私聊
        │                 （即用自己的号给自己发，见 owner_peer 配置）
        ├─ owner 回复 1/2/3 → 把对应候选发给 target_peer
        ├─ owner 回复 r     → 重新生成
        └─ owner 回复 d     → 丢弃本次待选

OneBot 服务端 → 本服务 的推送方式：HTTP POST（webhook）。
在 NapCat 的配置里把「HTTP 事件上报」指向本服务地址即可，无需 WebSocket 客户端。

安全约束（默认全部生效，可在配置里关）：
  * 只响应 owner 的审批指令，别人发数字不会触发发送
  * 只对 target_peer 的消息生成候选
  * 同一会话同一时刻只有一个待选项，避免误发
  * 所有发送都写审计日志（data/outbox/）
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Optional

from .config import Config, ROOT
from .engine import ReplyEngine, ReplyPlan, ReasoningError
from .faces import segments_to_text
from .llm import LLMError
from .memory import MemoryStore
from .schema import Message
from .senders import build_sender
from .senders.base import SendError

OUTBOX_DIR = ROOT / "data" / "outbox"

SELECT_HELP = (
    "回复数字选择要发送的候选：\n"
    "  1 = 情商线   2 = 直球线   3 = 稳一手\n"
    "  0 = 全权托管（模型直接回复，不再问你）\n"
    "  a = 切换持续托管（之后所有消息都自动回）\n"
    "  r = 重新生成\n"
    "  d = 丢弃本次\n"
    "多人同时来消息时，可在前面加 QQ 号指定对象，例如：\n"
    "  1987654321 1     （不加号则作用于最近收到的那条）\n"
)

#: 关闭自动托管的提示文案（在通知里带上）
AUTO_OFF_HINT = "n 可取消自动托管"


# --------------------------------------------------------------------------- #
# 号码解析
# --------------------------------------------------------------------------- #

def _looks_like_all(value: str) -> bool:
    """判断一个范围配置是否表示「所有人」。"""
    return (value or "").strip().lower() in ("", "*", "all", "any", "全部", "所有人")


def split_qq_numbers(value: str) -> set[str]:
    """从一段文本里解析出所有 QQ 号。

    支持逗号、空格、顿号、分号、换行、竖线分隔，例如：
        ``"1987654321, 2222222222"``
        ``"1987654321 2222222222"``
        ``"1987654321、2222222222"``

    只保留长度 >= 5 的纯数字串（QQ 号至少 5 位），
    这样可以安全地把 ``"*"``、``"all"``、说明文字一起忽略掉。
    """
    import re as _re

    out: set[str] = set()
    for token in _re.split(r"[,，、;；|\s]+", (value or "").strip()):
        token = token.strip()
        if token.isdigit() and len(token) >= 5:
            out.add(token)
    return out


def stop_model_if_configured(cfg: Config) -> None:
    """按配置在服务退出时停掉本地模型服务，释放显存。

    由 ``bot.stop_model_on_exit`` 控制，默认关闭。
    模型服务本身也有独立终端，关掉那个窗口同样会释放显存
    （supervisor 的 Job Object 保证 llama-server 不会变成孤儿进程）——
    这个开关只是给"习惯只关回复服务窗口"的人一个便利。
    """
    if not getattr(cfg.bot, "stop_model_on_exit", False):
        return
    script = Path(ROOT) / "tools" / "model_server.py"
    if not script.exists():
        return
    print("[退出] 按配置停止本地模型服务，释放显存…", flush=True)
    try:
        import subprocess
        import sys as _sys

        out = subprocess.run([_sys.executable, str(script), "stop"],
                             capture_output=True, text=True, timeout=90)
        for line in (out.stdout or "").splitlines():
            print("  " + line, flush=True)
    except Exception as e:
        print(f"[退出] 停止模型服务失败（忽略）：{e}", flush=True)


# --------------------------------------------------------------------------- #
# 待选状态
# --------------------------------------------------------------------------- #

@dataclass
class Pending:
    """一次等待 owner 审批的回复计划。"""

    peer_id: str                     # 要回复的对象（target_peer）
    plan: ReplyPlan
    incoming_text: str
    created_at: float = field(default_factory=time.time)
    #: 候选名称（随人格变化，如 一锐/一软/一退 或 情商线/直球线/稳一手）
    labels: list[str] = field(default_factory=lambda: ["候选1", "候选2", "候选3"])

    def summary(self) -> str:
        p = self.plan
        lines = [
            f"【待你选择 · 来自 {self.peer_id}】",
            f"对方说：{self.incoming_text}",
            "",
            f"① 读懂：{p.step1.literal}",
            f"   意图：{p.step1.intent}",
            f"② 情绪：{p.step2.emotion}（强度 {p.step2.intensity}）",
            f"   需求：{p.step2.need}",
            f"   雷区：{p.step2.risk}",
            f"③ 策略：{p.step3.strategy}",
            f"   腔调：{p.step3.tone}",
        ]
        if p.step3.avoid:
            lines.append(f"   避免：{'；'.join(p.step3.avoid)}")
        lines.append("")
        for i, c in enumerate(p.step3.candidates, 1):
            tag = self.labels[i - 1] if i <= len(self.labels) else f"候选{i}"
            body = c.text.replace("\n", " / ")
            lines.append(f"[{i}] {tag}｜{body}")
            if c.why:
                lines.append(f"     ↳ {c.why}")
        lines.append("")
        lines.append(SELECT_HELP)
        return "\n".join(lines)


# --------------------------------------------------------------------------- #
# 服务
# --------------------------------------------------------------------------- #

class BotService:
    """OneBot 事件处理器（与 HTTP 传输解耦，便于测试）。"""

    def __init__(self, cfg: Config, *, engine: Optional[ReplyEngine] = None,
                 sender: Any = None, log: Callable[[str], None] = print):
        self.cfg = cfg
        self.engine = engine or ReplyEngine(cfg)
        self.sender = sender or build_sender(cfg.sender, OUTBOX_DIR)
        self.log = log
        self.pending: dict[str, Pending] = {}      # peer -> 待选
        self._seen: set[str] = set()               # 去重
        self._lock = threading.Lock()
        #: 自动托管集合：这些对象的消息由模型直接回复，不再询问 owner
        #: 含 "*" 表示对所有人托管
        self.auto_peers: set[str] = set()
        #: 白名单文件缓存：(mtime, 号码集合)，用于热加载
        self._wl_cache: Optional[tuple[float, set[str]]] = None
        #: 「白名单为空」的提示只打一次，避免刷屏
        self._wl_warned = False

        #: 长期记忆（SQLite）。关掉时退化成"只在内存里记最近几轮"
        self.memory: Optional[MemoryStore] = None
        if getattr(cfg.memory, "enabled", False):
            try:
                self.memory = MemoryStore(cfg.memory.db_path)
                st = self.memory.stats()
                self.log(f"[记忆] 已加载 {self.memory.path.name}"
                         f"（{st['messages']} 条消息 / {st['facts']} 条事实）")
            except Exception as e:
                self.log(f"[记忆] 初始化失败，退回内存模式：{e}")
                self.memory = None
        #: 内存兜底：记忆关闭或写库失败时仍保留最近几轮
        self.history: dict[str, list[tuple[str, str]]] = {}
        self._history_limit = 20
        #: 距离上次抽取事实又积累了多少条消息
        self._since_extract = 0
        self._extracting = False

    # ---------------------------------------------------------------- 对话记忆
    def remember(self, peer_id: str, *, mine: bool, text: str,
                 msg_id: str = "", ts: Optional[float] = None) -> None:
        """记录一轮对话：写进长期记忆库，同时保留内存副本。"""
        if not (text or "").strip():
            return
        # 内存兜底（也保证记忆关闭时功能不退化）
        with self._lock:
            h = self.history.setdefault(peer_id, [])
            h.append(("我" if mine else "对方", text))
            if len(h) > self._history_limit:
                del h[: len(h) - self._history_limit]

        if self.memory is not None:
            try:
                self.memory.add_message(peer_id, mine=mine, text=text,
                                        msg_id=msg_id, ts=ts)
            except Exception as e:
                self.log(f"[记忆] 写入失败（不影响回复）：{e}")
        # 统计新消息，够了就后台抽事实
        if not mine:
            self._since_extract += 1
            maybe_extract(self, peer_id)

    def history_messages(self, peer_id: str, *, limit: Optional[int] = None
                         ) -> list[Message]:
        """短期上下文：优先用长期库里的最近若干轮。"""
        from .schema import Direction, Message as _Msg

        n = int(limit or getattr(self.cfg.memory, "recent_limit", 16) or 16)
        if self.memory is not None:
            try:
                rows = self.memory.recent(peer_id, n)
                if rows:
                    return [
                        _Msg(msg_id=f"m{r.row_id}", ts=r.ts,
                             direction=Direction.OUT if r.mine else Direction.IN,
                             peer_id=peer_id, text=r.text)
                        for r in rows
                    ]
            except Exception as e:
                self.log(f"[记忆] 读取最近消息失败：{e}")

        with self._lock:
            items = list(self.history.get(peer_id, []))[-n:]
        return [
            _Msg(msg_id=f"h{i}", ts=0,
                 direction=Direction.OUT if who == "我" else Direction.IN,
                 peer_id=peer_id, text=text)
            for i, (who, text) in enumerate(items)
        ]

    def build_memory_context(self, peer_id: str, incoming: str) -> dict[str, Any]:
        """为这一轮准备长期记忆上下文。

        关键设计（踩坑后调整）：**不要把所有事实都塞进去**。
        早期版本把最近 20 条事实全部注入，结果 7B 模型开始"硬塞"——
        对方说吃火锅，它却去聊面试。现在改成：

            * 身份/关系类事实（叫什么、做什么、什么关系）总是注入，
              因为它们对任何话题都成立，且风险低；
            * 其余事实只在**与当前消息关键词相关**时才注入；
            * 总量设上限，避免提示词被记忆淹没。

        返回值交给引擎拼进提示词。任何异常都吞掉——记忆坏了也不该
        让回复失败。
        """
        ctx: dict[str, Any] = {"old_messages": [], "facts": []}
        if self.memory is None:
            return ctx
        try:
            recent = self.memory.recent(peer_id, int(
                getattr(self.cfg.memory, "recent_limit", 16) or 16))
            oldest_ts = recent[0].ts if recent else None
            exclude = {r.row_id for r in recent}

            # 1) 按当前消息的关键词召回更早的相关内容
            recall_n = int(getattr(self.cfg.memory, "recall_limit", 6) or 0)
            if recall_n > 0:
                ctx["old_messages"] = self.memory.recall(
                    peer_id, incoming, limit=recall_n,
                    before_ts=oldest_ts, exclude_ids=exclude)

            # 2) 事实：身份类总是给，其余按相关性给
            core_n = int(getattr(self.cfg.memory, "core_fact_limit", 4) or 0)
            rel_n = int(getattr(self.cfg.memory, "relevant_fact_limit", 4) or 0)
            core_kinds = tuple(getattr(self.cfg.memory, "core_fact_kinds",
                                       ("身份", "关系")) or ())
            facts: list = []
            seen: set[str] = set()

            if core_n > 0:
                for f in self.memory.facts(peer_id, limit=40, kinds=core_kinds):
                    if f.content in seen:
                        continue
                    facts.append(f)
                    seen.add(f.content)
                    if len(facts) >= core_n:
                        break

            if rel_n > 0:
                for f in self.memory.recall_facts(peer_id, incoming,
                                                  limit=rel_n * 2):
                    if f.content in seen:
                        continue
                    facts.append(f)
                    seen.add(f.content)
                    if len(facts) >= core_n + rel_n:
                        break

            ctx["facts"] = facts
            if facts:
                self.memory.bump_fact_hits(f.row_id for f in facts)
        except Exception as e:
            self.log(f"[记忆] 组装上下文失败（忽略）：{e}")
        return ctx

    # ---------------------------------------------------------------- 表情学习
    def learn_faces(self, learned: dict[str, str]) -> None:
        """把这一轮新认识的表情 id 存进缓存。"""
        if not learned or self.memory is None:
            return
        for fid, nm in learned.items():
            try:
                self.memory.learn_face(fid, nm)
            except Exception:
                pass
        self.log(f"[表情] 新认识 {len(learned)} 个："
                 + "、".join(f"{k}={v}" for k, v in list(learned.items())[:5]))

    # ---------------------------------------------------------------- 发送延迟
    def _human_delay(self) -> float:
        """返回本次发送前应等待的随机秒数（模拟真人打字节奏）。"""
        import random

        lo = float(getattr(self.cfg.bot, "send_delay_min", 0) or 0)
        hi = float(getattr(self.cfg.bot, "send_delay_max", 0) or 0)
        if hi <= 0 or hi < lo:
            return 0.0
        return random.uniform(lo, hi)

    def _sleep_before_send(self, reason: str = "") -> None:
        d = self._human_delay()
        if d <= 0:
            return
        self.log(f"[延迟] 模拟打字，等待 {d:.1f} 秒后发送{('（' + reason + '）') if reason else ''}")
        time.sleep(d)

    # ---------------------------------------------------------------- 配置便捷
    @property
    def target_peer(self) -> str:
        return str(self.cfg.bot.target_peer or self.cfg.peer or "").strip()

    @property
    def target_mode(self) -> str:
        """回复范围模式：``"all"`` 或 ``"whitelist"``。

        显式配置优先；未配置时按 ``target_peer`` 推断，
        这样 ``target_peer: "*"`` 与旧的 ``"1987654321"`` 写法都能照旧工作。
        """
        explicit = str(getattr(self.cfg.bot, "target_mode", "") or "").strip().lower()
        if explicit in ("all", "any", "*"):
            return "all"
        if explicit in ("whitelist", "list", "white"):
            return "whitelist"
        return "all" if _looks_like_all(self.target_peer) else "whitelist"

    @property
    def target_all(self) -> bool:
        """目标是否为「任何人」。"""
        return self.target_mode == "all"

    # ---- 白名单 ----
    def whitelist_path(self) -> Optional[Path]:
        """白名单文件的绝对路径；未配置则返回 None。"""
        raw = str(getattr(self.cfg.bot, "whitelist_file", "") or "").strip()
        if not raw:
            return None
        p = Path(raw)
        return p if p.is_absolute() else (ROOT / p)

    def load_whitelist(self, *, force: bool = False) -> set[str]:
        """当前白名单里的所有 QQ 号（``target_peer`` 里的号 ∪ 白名单文件里的号）。

        白名单文件按 mtime 缓存：**改完存盘立即生效，无需重启服务**。
        文件不存在或读不了时，只返回 ``target_peer`` 里的号（不抛异常）。
        """
        numbers = split_qq_numbers(self.target_peer)
        path = self.whitelist_path()
        if path is None:
            return numbers
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return numbers          # 文件还没建，不算错误
        if not force and self._wl_cache is not None and self._wl_cache[0] == mtime:
            return numbers | self._wl_cache[1]

        found: set[str] = set()
        try:
            text = path.read_text(encoding="utf-8-sig")
        except OSError as e:
            self.log(f"[白名单] 读取失败，忽略：{path}（{e}）")
            return numbers
        for raw_line in text.splitlines():
            line = raw_line.split("#", 1)[0].strip()
            if line:
                found.update(split_qq_numbers(line))
        self._wl_cache = (mtime, found)
        return numbers | found

    def _is_target(self, user_id: str) -> bool:
        """判断某个发信人是否要生成候选。

        owner 自己永远排除——他的消息只当审批指令用，不该生成候选。
        白名单模式下名单为空则**谁也不回**（宁可漏，不可滥发）。
        """
        if self.owner and user_id == self.owner:
            return False
        if self.target_mode == "all":
            return True

        allowed = self.load_whitelist()
        if not allowed:
            if not self._wl_warned:
                self._wl_warned = True
                path = self.whitelist_path()
                self.log(f"[白名单] 名单为空，当前不回复任何人。"
                         f"请往 {path} 里填入 QQ 号（一行一个），"
                         f"或把 bot.target_mode 改成 all。")
            return False
        return user_id in allowed

    def _is_auto(self, peer_id: str) -> bool:
        """该对象是否处于自动托管（``"*"`` 表示对所有人托管）。"""
        return "*" in self.auto_peers or peer_id in self.auto_peers

    @property
    def owner(self) -> str:
        return str(self.cfg.bot.owner or self.cfg.sender.self_id or "")

    def _peer_label(self, peer_id: str) -> str:
        """展示用的对象名：托管通配时也显示具体号。"""
        return peer_id or "任何人"

    # ---------------------------------------------------------------- 事件入口
    def handle_event(self, event: dict[str, Any]) -> dict[str, Any]:
        """处理一条 OneBot 事件，返回处理结果（便于测试与日志）。"""
        post_type = event.get("post_type")
        # 详细事件日志：排查「收到上报但不生成候选」时最关键的一行。
        # 心跳类事件噪声大，单独压成一行；消息类事件打印全貌。
        if post_type == "meta_event":
            if self.cfg.bot.log_events:
                self.log(f"[事件] 心跳 {event.get('meta_event_type')}"
                         f"（忽略：不是消息）")
        elif self.cfg.bot.log_events:
            mt = event.get("message_type") or "-"
            uid = event.get("user_id") or "-"
            sid = event.get("self_id") or "-"
            preview = ""
            if post_type == "message":
                raw = event.get("message")
                if isinstance(raw, list):
                    preview = "".join(
                        str(s.get("data", {}).get("text") or "")
                        for s in raw if isinstance(s, dict) and s.get("type") == "text"
                    )
                else:
                    preview = str(raw or "")
            self.log(f"[事件] post_type={post_type} message_type={mt} "
                     f"from={uid} self={sid} text={preview[:60]!r}")

        if post_type == "message" or "message_type" in event:
            result = self._on_message(event)
            if self.cfg.bot.log_events and not result.get("handled"):
                self.log(f"[跳过] {result.get('reason')}")
            return result
        if post_type in ("meta_event", "notice", "request"):
            return {"handled": False, "reason": f"忽略 {post_type}"}
        return {"handled": False, "reason": "未知事件类型"}

    def _on_message(self, event: dict[str, Any]) -> dict[str, Any]:
        if event.get("message_type") != "private":
            return {"handled": False, "reason": "只处理私聊消息"}

        user_id = str(event.get("user_id") or "")

        # 解析消息段：表情/表情包/图片/语音 都会翻成文字，
        # 不再出现"对方只发了个表情 -> 被当成空消息丢掉"。
        learned: dict[str, str] = {}
        parsed = segments_to_text(event.get("message"), learn=learned)
        text = parsed.text
        if learned:
            self.learn_faces(learned)

        # 纯图片/语音这类没有文字的消息：用占位文案兜住，仍然走后续逻辑，
        # 这样对方发张图你也能收到候选（而不是静默无反应）。
        if not text.strip():
            if parsed.has_content or parsed.kinds:
                text = "（对方发来一条非文字消息）"
            else:
                return {"handled": False, "reason": "没有可解析的内容"}

        if not user_id:
            return {"handled": False, "reason": "缺少发送者"}

        # 记录这一轮的段类型，便于排查（尤其表情/图片）
        if self.cfg.bot.log_events and len(parsed.kinds) > 1:
            self.log(f"[解析] 段类型={parsed.kinds} 表情id={parsed.face_ids} "
                     f"-> {text[:60]!r}")

        # 去重：同一 message_id 只处理一次
        mid = str(event.get("message_id") or "")
        if mid:
            with self._lock:
                if mid in self._seen:
                    return {"handled": False, "reason": "重复事件"}
                self._seen.add(mid)
                if len(self._seen) > 5000:      # 控制内存
                    self._seen = set(list(self._seen)[-2000:])

        # 1) owner 的审批指令优先
        if self.owner and user_id == self.owner:
            handled = self._on_owner_command(text)
            if handled is not None:
                return handled

        # 2) 目标对象的消息 -> 生成候选
        #    范围由 bot.target_mode 决定：all = 谁都接，whitelist = 只接名单里的号。
        if self._is_target(user_id):
            return self._on_target_message(user_id, text, msg_id=mid)

        who = ("任何人" if self.target_all
               else f"白名单（{len(self.load_whitelist())} 个号）")
        return {"handled": False,
                "reason": f"不在回复范围（from={user_id}，范围={who}）"}

    # ---------------------------------------------------------------- 生成候选
    def _plan_with_memory(self, peer_id: str, text: str):
        """统一的生成入口：带上少量短期上下文 + 长期记忆。

        历史刻意只给几轮（``memory.prompt_history_limit``）：给多了模型会被
        最近话题锚住，反而不用相关性召回的记忆。详见该配置项的注释。
        """
        ctx = self.build_memory_context(peer_id, text)
        if ctx["old_messages"] or ctx["facts"]:
            self.log(f"[记忆] 召回 {len(ctx['old_messages'])} 条旧消息 / "
                     f"{len(ctx['facts'])} 条事实")
        n_hist = int(getattr(self.cfg.memory, "prompt_history_limit", 4) or 0)
        hist = self.history_messages(peer_id, limit=n_hist) if n_hist else None
        return self.engine.plan(text, peer_id=peer_id, history=hist, memory=ctx)

    def _on_target_message(self, peer_id: str, text: str, *,
                           msg_id: str = "", store: bool = True
                           ) -> dict[str, Any]:
        """生成候选。

        Args:
            msg_id: OneBot 的消息 id，用于长期记忆去重（同一句只存一次）
            store: False 表示只生成不落库（重新生成时用，避免重复存储）
        """
        self.log(f"[收到] {peer_id}: {text}")
        if store:
            self.remember(peer_id, mine=False, text=text, msg_id=msg_id)

        # 自动托管：模型全权回复，不再询问 owner
        if self._is_auto(peer_id):
            return self._auto_reply(peer_id, text)

        try:
            plan = self._plan_with_memory(peer_id, text)
        except (ReasoningError, LLMError) as e:
            self.log(f"[错误] 生成候选失败：{e}")
            self._notify_owner(f"⚠ 生成候选失败：{e}\n对方（{peer_id}）说：{text}")
            return {"handled": True, "action": "generate_failed", "error": str(e)}

        pending = Pending(peer_id=peer_id, plan=plan, incoming_text=text,
                          labels=self.engine.candidate_labels())
        with self._lock:
            self.pending[peer_id] = pending

        ok = self._notify_owner(pending.summary())
        return {"handled": True, "action": "candidates_sent",
                "peer_id": peer_id, "notified": ok}

    def _auto_reply(self, peer_id: str, text: str) -> dict[str, Any]:
        """自动托管模式：让模型自己挑一条发出去（带拟人延迟）。"""
        self.log(f"[自动托管] 正在为 {peer_id} 生成回复…")
        try:
            plan = self._plan_with_memory(peer_id, text)
        except (ReasoningError, LLMError) as e:
            self.log(f"[自动托管] 生成失败：{e}")
            self._notify_owner(f"⚠ 自动回复失败：{e}\n对方（{peer_id}）说：{text}")
            return {"handled": True, "action": "auto_failed", "error": str(e)}

        cands = plan.candidates()
        if not cands:
            return {"handled": True, "action": "auto_failed", "error": "没有候选"}

        # 默认自动选第一条（人格里的第一候选就是最推荐的）
        chosen = cands[0]
        labels = self.engine.candidate_labels()
        tag = labels[0] if labels else "候选1"
        self.log(f"[自动托管] 选定【{tag}】：{chosen.text}")
        self._notify_owner(
            f"【自动托管 · 已自动回复】\n"
            f"对象：{peer_id}\n"
            f"对方说：{text}\n"
            f"规则：{plan.step2.emotion} / {plan.step3.strategy}\n"
            f"发出：【{tag}】{chosen.text}\n"
            f"（回复 {AUTO_OFF_HINT}）"
        )
        ok = self._send(peer_id, chosen.text, delay_reason="自动托管")
        return {"handled": True, "action": "auto_sent" if ok else "auto_send_failed",
                "peer_id": peer_id, "text": chosen.text, "ok": ok}

    # ---------------------------------------------------------------- 审批
    @staticmethod
    def _parse_command(text: str) -> tuple[str, str]:
        """把 owner 的输入拆成 ``(指定对象, 命令)``。

        支持两种写法：
            ``1``                 -> ("", "1")            作用于最近一条待选
            ``1987654321 1``      -> ("1987654321", "1")  显式指定对象

        多人同时有待选时，后者能避免选错人（QQ 号至少 5 位，
        所以纯数字短输入不会被误判成对象）。
        """
        s = (text or "").strip()
        parts = s.split(None, 1)
        if len(parts) == 2 and parts[0].isdigit() and len(parts[0]) >= 5:
            return parts[0], parts[1].strip()
        return "", s

    def _on_owner_command(self, text: str) -> Optional[dict[str, Any]]:
        """owner 的私聊内容。返回 None 表示不是审批指令，交给后续逻辑。"""
        explicit_peer, raw_cmd = self._parse_command(text)
        cmd = raw_cmd.strip().lower()

        # ---- 自动托管控制（不依赖是否有待选，优先处理）----
        # "a"：切换持续托管；"n"：取消托管
        if self.cfg.bot.allow_auto_reply and cmd in ("a", "自动", "auto"):
            # 没有指定对象时：有待选就取最近的；无人待选且目标是「任何人」，
            # 则开启对所有人的托管（用 "*" 标记）。
            peer = explicit_peer or self._current_peer()
            if not peer and self.target_all:
                peer = "*"
            if not peer:
                self._notify_owner("当前没有指定回复对象，无法开启托管。")
                return {"handled": True, "action": "auto_toggle_no_peer"}

            if peer in self.auto_peers:
                self.auto_peers.discard(peer)
                self.log(f"[自动托管] 已取消：{peer}")
                self._notify_owner(f"已取消自动托管（{self._peer_label(peer)}）。"
                                   f"之后会照常发候选给你选。")
                return {"handled": True, "action": "auto_off", "peer_id": peer}
            self.auto_peers.add(peer)
            self.log(f"[自动托管] 已开启：{peer}")
            who = "所有人" if peer == "*" else peer
            self._notify_owner(
                f"已开启【持续自动托管】（{who}）。\n"
                f"之后{'任何人' if peer == '*' else '对方'}发来的消息，"
                f"我会直接替我回复，并同步告诉你发了什么。\n"
                f"回复 n 可随时取消。")
            return {"handled": True, "action": "auto_on", "peer_id": peer}

        if cmd in ("n", "取消托管", "stop"):
            if self.auto_peers:
                peers = ", ".join(sorted(self.auto_peers))
                self.auto_peers.clear()
                self.log(f"[自动托管] 已全部取消：{peers}")
                self._notify_owner(f"已取消自动托管（{peers}）。")
                return {"handled": True, "action": "auto_off_all"}
            return None

        # ---- 以下指令都需要一条待选 ----
        if explicit_peer:
            with self._lock:
                pending = self.pending.get(explicit_peer)
            if pending is None:
                self._notify_owner(f"没有来自 {explicit_peer} 的待选消息。")
                return {"handled": True, "action": "no_pending_for_peer",
                        "peer_id": explicit_peer}
            peer_id = explicit_peer
        else:
            with self._lock:
                if not self.pending:
                    return None
                # 未指定对象时，作用于最近收到的那条
                peer_id = max(self.pending, key=lambda k: self.pending[k].created_at)
                pending = self.pending[peer_id]

        # "0"：本次交给模型全权回复（发出去，并进入托管）
        if cmd == "0":
            if not self.cfg.bot.allow_auto_reply:
                self._notify_owner("自动回复功能已在配置里关闭（bot.allow_auto_reply）。")
                return {"handled": True, "action": "auto_disabled"}
            with self._lock:
                self.pending.pop(peer_id, None)
            self.auto_peers.add(peer_id)
            self.log(f"[自动托管] 本次交给模型全权回复，并开启托管：{peer_id}")
            res = self._auto_reply(peer_id, pending.incoming_text)
            res["auto_mode"] = "on"
            return res

        if cmd in ("d", "丢弃", "drop", "cancel"):
            with self._lock:
                self.pending.pop(peer_id, None)
            self.log(f"[丢弃] 取消对 {peer_id} 的回复")
            self._notify_owner(f"已丢弃对 {peer_id} 的回复。")
            return {"handled": True, "action": "dropped", "peer_id": peer_id}

        if cmd in ("r", "重新生成", "regen", "retry"):
            self.log(f"[重新生成] {peer_id}")
            # store=False：重新生成不该把同一句话再存一遍记忆
            return self._on_target_message(peer_id, pending.incoming_text,
                                           store=False)

        if cmd.isdigit():
            idx = int(cmd)
            cands = pending.plan.candidates()
            if not (1 <= idx <= len(cands)):
                self._notify_owner(f"序号 {idx} 超出范围（可选 1-{len(cands)}）。")
                return {"handled": True, "action": "invalid_index", "index": idx}
            chosen = cands[idx - 1].text
            res = self._send(peer_id, chosen, delay_reason="你选的第%d条" % idx)
            if res:
                with self._lock:
                    self.pending.pop(peer_id, None)
            return {"handled": True, "action": "sent" if res else "send_failed",
                    "peer_id": peer_id, "index": idx, "ok": res}

        return None

    def _current_peer(self) -> str:
        """当前正在对话的对象：有待选就用最近那条，否则用配置的目标对象。

        目标为「任何人」时返回空串——调用方需自行处理这种语义。
        """
        with self._lock:
            if self.pending:
                return max(self.pending, key=lambda k: self.pending[k].created_at)
        return "" if self.target_all else self.target_peer

    # ---------------------------------------------------------------- 发送
    def _send(self, peer_id: str, text: str, *, delay_reason: str = "") -> bool:
        """发送前按配置随机等待（模拟真人打字节奏）。"""
        self._sleep_before_send(delay_reason)
        try:
            res = self.sender.send_text(peer_id, text)
        except SendError as e:
            self.log(f"[发送失败] {e}")
            self._notify_owner(f"⚠ 发送给 {peer_id} 失败：{e}")
            return False
        if res.ok:
            self.log(f"[已发送] {peer_id}: {text}")
            self.remember(peer_id, mine=True, text=text)
            self._notify_owner(f"✓ 已发送给 {peer_id}：\n{text}")
            return True
        self.log(f"[发送失败] {res.detail}")
        self._notify_owner(f"⚠ 发送失败：{res.detail}")
        return False

    def _notify_owner(self, text: str) -> bool:
        """把文本发给 owner 自己（用于展示候选与结果）。

        owner_sink 取值：
          - "self"   ：发给自己的号（NapCat 支持给自己发私聊）
          - 具体 QQ  ：发给指定的号
          - ""       ：只写日志，不外发
        """
        sink = str(self.cfg.bot.owner_sink or "").strip()
        if not sink:
            self.log("[通知·仅日志]\n" + text)
            return False
        target = self.owner if sink == "self" else sink
        if not target:
            self.log("[通知·无 owner，仅日志]\n" + text)
            return False
        try:
            res = self.sender.send_text(target, text)
        except SendError as e:
            self.log(f"[通知失败] {e}")
            return False
        if not res.ok:
            self.log(f"[通知失败] {res.detail}")
        return bool(res.ok)


# --------------------------------------------------------------------------- #
# HTTP 传输层（OneBot webhook）
# --------------------------------------------------------------------------- #

def _extract_text(message: Any, *, learn: Optional[dict] = None) -> str:
    """从 OneBot 的 message 字段取出可读文本。

    兼容 string 形式与 array 形式。除了 text 段，还会翻译：
    表情（face，查内置 399 个 id 对照表）、表情包（mface）、图片（image，
    取自带 summary 或标注）、语音、@、引用、卡片等。

    旧实现只取 text 段，导致"对方只发了个表情"被当成空消息直接丢弃。
    """
    return segments_to_text(message, learn=learn).text


def maybe_extract(service: "BotService", peer_id: str) -> None:
    """够量了就后台抽取长期事实（不阻塞回复）。

    为什么放后台：抽事实要调一次模型，慢的话会拖住回复。
    抽失败也无所谓，下一轮还会再试。
    """
    mc = service.cfg.memory
    if service.memory is None or not getattr(mc, "extract_facts", False):
        return
    every = max(2, int(getattr(mc, "extract_every", 6) or 6))
    if service._since_extract < every or service._extracting:
        return
    service._since_extract = 0
    service._extracting = True

    def _work() -> None:
        try:
            extract_facts(service, peer_id)
        except Exception as e:
            service.log(f"[记忆] 抽取事实失败（忽略）：{e}")
        finally:
            service._extracting = False

    threading.Thread(target=_work, name="fact-extract", daemon=True).start()


_EXTRACT_SYSTEM = (
    "你从聊天记录里抽取关于「对方」的**长期有用的事实**，供以后聊天参考。\n"
    "\n"
    "只抽这些类型：\n"
    "  身份  对方的称呼、职业、城市、家庭关系\n"
    "  偏好  喜欢/讨厌什么、口味、习惯\n"
    "  事件  有具体时间的安排（面试、生日、旅行、考试）\n"
    "  关系  我们之间的约定、称呼、重要日子、矛盾\n"
    "  其他  值得记住的零碎信息\n"
    "\n"
    "严格要求：\n"
    "1. 只抽**对方明确说过**的，不要推测、不要脑补。\n"
    "2. 不要抽一次性的寒暄（「在吗」「哈哈」「吃了吗」）。\n"
    "3. 每条事实独立完整、能脱离上下文看懂，20 字以内。\n"
    "4. 已被现有事实覆盖的就不要再抽。\n"
    "5. 没有值得记的就返回空数组。\n"
    "\n"
    '只输出 JSON：{"facts":[{"kind":"身份","content":"..."}]}'
)


def extract_facts(service: "BotService", peer_id: str, *,
                  window: int = 24) -> int:
    """从最近的对话里抽取长期事实并入库，返回新增条数。"""
    if service.memory is None:
        return 0
    rows = service.memory.recent(peer_id, window)
    if len(rows) < 4:
        return 0

    convo = "\n".join(r.line() for r in rows)
    existing = service.memory.facts(peer_id, limit=60)
    already = ("\n已有事实（不要重复）：\n"
               + "\n".join(f"- {f.line()}" for f in existing)) if existing else ""

    user = f"最近的聊天记录：\n{convo}\n{already}\n\n请抽取长期事实。"
    try:
        data = service.engine.client.chat_json(
            _EXTRACT_SYSTEM, user,
            temperature=float(getattr(service.cfg.memory,
                                      "extract_temperature", 0.2)),
            max_tokens=600)
    except (LLMError, ReasoningError) as e:
        service.log(f"[记忆] 抽取调用失败：{e}")
        return 0

    items = data.get("facts") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return 0

    added = 0
    for it in items:
        if isinstance(it, str):
            kind, content = "其他", it
        elif isinstance(it, dict):
            kind = str(it.get("kind") or "其他").strip() or "其他"
            content = str(it.get("content") or it.get("fact") or "").strip()
        else:
            continue
        if not content or len(content) > 60:
            continue
        try:
            if service.memory.add_fact(peer_id, kind, content):
                added += 1
        except Exception:
            pass
    if added:
        service.log(f"[记忆] 新增 {added} 条长期事实（{peer_id}）")
    return added


class _Handler(BaseHTTPRequestHandler):
    service: BotService = None          # type: ignore[assignment]
    access_token: str = ""

    server_version = "luxun-qq/0.2"

    def do_GET(self) -> None:  # noqa: N802
        if self.path.rstrip("/") in ("", "/health", "/ping"):
            self._json(200, {"status": "ok", "service": "luxun-qq",
                             "pending": len(self.service.pending)})
            return
        self._json(404, {"status": "not_found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.access_token:
            auth = self.headers.get("Authorization") or ""
            if auth != f"Bearer {self.access_token}":
                self._json(401, {"status": "unauthorized"})
                return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            event = json.loads(raw.decode("utf-8"))
            if not isinstance(event, dict):
                event = {"_non_dict": event}
        except Exception as e:
            self._json(400, {"status": "bad_request", "error": str(e)})
            return

        # 排查用：把原始 body 的长度与预览写进日志。
        # 空 body / 非预期格式是「收到 POST 但没反应」最常见的原因。
        if getattr(self.service.cfg.bot, "log_raw_body", False):
            preview = raw[:300].decode("utf-8", "ignore")
            # 关键：打印请求头，用来确认请求到底是不是 NapCat 发的
            # （NapCat 的 httpClient 会带 x-self-id 头）
            interesting = {k: v for k, v in self.headers.items()
                           if k.lower() in ("content-type", "x-self-id",
                                            "x-signature", "user-agent",
                                            "content-length")}
            self.service.log(f"[原始] len={len(raw)} headers={interesting} "
                             f"body={preview!r}")

        try:
            result = self.service.handle_event(event)
        except Exception as e:                      # 兜底，绝不让服务挂掉
            self.service.log(f"[异常] 处理事件出错：{type(e).__name__}: {e}")
            result = {"handled": False, "error": str(e)}
        self._json(200, {"status": "ok", "result": result})

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: Any) -> None:
        if self.service:
            self.service.log("[http] " + (fmt % args))


def make_server(cfg: Config, service: BotService, *,
                host: Optional[str] = None, port: Optional[int] = None,
                access_token: Optional[str] = None) -> ThreadingHTTPServer:
    """构造 OneBot webhook 服务端。"""
    h = host or cfg.bot.host
    p = port or cfg.bot.port
    token = cfg.bot.access_token if access_token is None else access_token

    class Handler(_Handler):
        pass

    Handler.service = service
    Handler.access_token = token
    srv = ThreadingHTTPServer((h, p), Handler)
    srv.daemon_threads = True
    return srv


def run(cfg: Config) -> int:
    """启动服务（阻塞）。"""
    service = BotService(cfg)
    srv = make_server(cfg, service)
    host, port = srv.server_address[:2]
    print("=" * 66)
    print("luxun-qq 自动回复服务已启动")
    print("=" * 66)
    print(f"  监听        : http://{host}:{port}")
    print(f"  我的号      : {cfg.sender.self_id}")
    print(f"  回复对象    : {service.target_peer or '(未设置 target_peer)'}")
    print(f"  owner       : {service.owner}")
    print(f"  候选投递到  : {cfg.bot.owner_sink or '(仅日志)'}")
    print(f"  发送适配器  : {cfg.sender.adapter}")
    print(f"  推理后端    : {cfg.llm.backend} / {cfg.llm.model}")
    print()
    print("在 NapCat 里把「HTTP 事件上报」指向上面的地址，路径任意（本服务收所有 POST）。")
    print("Ctrl+C 停止。")
    print("=" * 66)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n正在停止…")
    finally:
        srv.shutdown()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(run(Config.load()))
