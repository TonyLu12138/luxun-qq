# -*- coding: utf-8 -*-
"""luxun-qq 命令行入口。

主流程（用户要求的形态）：
    对方消息 ──▶ 三步思考（读懂 / 析情绪 / 定策略）──▶ 三个候选 ──▶ 我选一个 ──▶ 自动发送

用法：
    python -m luxun_qq plan  "对方说的话"          # 只出方案，不发送
    python -m luxun_qq plan  --file msg.txt --send  # 出方案并交互选择后发送
    python -m luxun_qq chat                        # 交互式连续对话
    python -m luxun_qq ask                         # 管道输入
    python -m luxun_qq check                       # 自检后端与发送适配器
    python -m luxun_qq style --sample me.txt       # 生成风格指南
    python -m luxun_qq send --peer 123 --text "…"  # 直接发送（不经过模型）
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

from .config import CORPUS_DIR, ROOT, Config
from .engine import ReplyEngine, ReplyPlan, ReasoningError, fallback_plan
from .llm import LLMError, build_client
from .persona import LUXUN_PERSONA, build_style_guide_raw, load_style_sample
from .schema import Direction, Message, read_messages
from .senders import build_sender
from .senders.base import SendError

OUTBOX_DIR = ROOT / "data" / "outbox"


# --------------------------------------------------------------------------- #
# 组装
# --------------------------------------------------------------------------- #

def make_engine(cfg: Config) -> ReplyEngine:
    """按配置组装引擎：客户端 + 风格指南 + 可选检索。"""
    client = build_client(cfg.llm)

    guide_parts: list[str] = []
    sample = load_style_sample(cfg.retrieve.style_sample)
    if sample:
        guide_parts.append(sample)

    retriever = None
    idx = Path(cfg.retrieve.index_path)
    if cfg.retrieve.enabled and idx.exists():
        try:
            from .corpus import StyleRetriever

            retriever = StyleRetriever.load(idx)
            stats_path = Path(cfg.retrieve.stats_path)
            if stats_path.exists():
                guide_parts.append(
                    build_style_guide_raw(json.loads(stats_path.read_text("utf-8"))))
        except Exception as e:  # 检索是增强项，失败不能挡住主流程
            print(f"[warn] 风格检索不可用，已忽略：{e}", file=sys.stderr)

    return ReplyEngine(cfg, client=client, retriever=retriever,
                       style_guide="\n\n".join(guide_parts), persona=LUXUN_PERSONA)


def make_sender(cfg: Config):
    return build_sender(cfg.sender, OUTBOX_DIR)


# --------------------------------------------------------------------------- #
# 展示与选择
# --------------------------------------------------------------------------- #

def print_plan(plan: ReplyPlan, labels: list[str] | None = None) -> None:
    print()
    print("┌─ 第一步 · 读懂对方 " + "─" * 42)
    print(f"│ 字面：{plan.step1.literal}")
    print(f"│ 意图：{plan.step1.intent}")
    for f in plan.step1.facts:
        print(f"│ 事实：{f}")
    print("├─ 第二步 · 分析情绪 " + "─" * 42)
    print(f"│ 情绪：{plan.step2.emotion}（强度 {plan.step2.intensity}）")
    print(f"│ 需求：{plan.step2.need}")
    print(f"│ 雷区：{plan.step2.risk}")
    print("├─ 第三步 · 回复策略 " + "─" * 42)
    print(f"│ 策略：{plan.step3.strategy}")
    print(f"│ 腔调：{plan.step3.tone}")
    if plan.step3.avoid:
        print(f"│ 避免：{'；'.join(plan.step3.avoid)}")
    print("└─ 候选回复 " + "─" * 48)
    # 标签随人格变化（情感大师是 情商线/直球线/稳一手，锐评腔是一锐/一软/一退）
    tags = list(labels or []) or ["一锐", "一软", "一退", "备用"]
    for i, c in enumerate(plan.step3.candidates, 1):
        tag = tags[i - 1] if i <= len(tags) else f"候选{i}"
        body = c.text.replace("\n", " ⏎ ")
        print(f"  [{i}] {tag}｜{body}")
        if c.why:
            print(f"       ↳ {c.why}")
    if plan.degraded:
        print(f"\n⚠ 降级模式：{plan.note}")
    print()


def choose(plan: ReplyPlan, *, allow_edit: bool = True) -> Optional[str]:
    """交互选择。返回要发送的文本；None 表示放弃。"""
    n = len(plan.step3.candidates)
    if n == 0:
        return None
    prompt = (f"选择要发送的候选 [1-{n}]，r=重新生成，e=自己改写，"
              f"回车=放弃，q=退出：")
    try:
        raw = input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return None

    if raw == "":
        return None
    if raw.lower() == "q":
        raise SystemExit(0)
    if raw.lower() == "r":
        return ""            # 空串作为"重新生成"信号
    if raw.lower() == "e":
        if not allow_edit:
            return None
        try:
            text = input("输入你想发的内容（空行放弃）：").rstrip()
        except (EOFError, KeyboardInterrupt):
            return None
        return text or None
    if raw.isdigit() and 1 <= int(raw) <= n:
        return plan.step3.candidates[int(raw) - 1].text
    print("输入无法识别，已放弃。")
    return None


def send_chosen(cfg: Config, peer: str, text: str) -> bool:
    sender = make_sender(cfg)
    try:
        res = sender.send_text(peer, text)
    except SendError as e:
        print(f"✗ 发送失败：{e}", file=sys.stderr)
        return False
    if res.ok:
        print(f"✓ 已通过 [{res.adapter}] 发送给 {peer}：{text}")
        return True
    print(f"✗ 发送失败：{res.detail}", file=sys.stderr)
    return False


# --------------------------------------------------------------------------- #
# 子命令
# --------------------------------------------------------------------------- #

def cmd_plan(args, cfg: Config) -> int:
    text = args.text
    if args.file:
        text = Path(args.file).read_text(encoding="utf-8").strip()
    if not text:
        text = sys.stdin.read().strip()
    if not text:
        print("请提供对方的消息（位置参数 / --file / stdin）。", file=sys.stderr)
        return 2

    peer = args.peer or cfg.peer or ""
    engine = make_engine(cfg)

    while True:
        try:
            plan = engine.plan(text, peer_id=peer)
        except (ReasoningError, LLMError) as e:
            print(f"✗ 生成失败：{e}", file=sys.stderr)
            if args.fallback:
                plan = fallback_plan(text, str(e))
                print_plan(plan, engine.candidate_labels())
            return 1

        print_plan(plan, engine.candidate_labels())
        if args.json:
            print(json.dumps(plan.to_dict(), ensure_ascii=False, indent=2))

        if not args.send:
            return 0
        if not peer:
            print("发送需要知道发给谁：请设置 config.yaml 的 peer，或加 --peer。",
                  file=sys.stderr)
            return 2

        chosen = choose(plan)
        if chosen == "":            # 重新生成
            continue
        if chosen is None:
            print("未发送。")
            return 0
        return 0 if send_chosen(cfg, peer, chosen) else 1


def cmd_chat(args, cfg: Config) -> int:
    peer = args.peer or cfg.peer or ""
    engine = make_engine(cfg)
    history: list[Message] = []
    print("交互模式：粘贴对方的话回车；:q 退出；:peer <qq> 切换对象。")
    if not peer:
        print("提示：未设置 peer，发送前会用 --peer 或 config.yaml 的 peer。")
    while True:
        try:
            text = input("\n对方> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not text:
            continue
        if text in (":q", ":quit", ":exit"):
            return 0
        if text.startswith(":peer "):
            peer = text.split(None, 1)[1].strip()
            print(f"已切换对话对象：{peer}")
            continue
        try:
            plan = engine.plan(text, history=history or None, peer_id=peer)
        except (ReasoningError, LLMError) as e:
            print(f"✗ 生成失败：{e}", file=sys.stderr)
            continue
        print_plan(plan, engine.candidate_labels())
        chosen = choose(plan)
        if chosen == "":
            continue
        history.append(Message(msg_id=f"in{len(history)}", ts=0,
                               direction=Direction.IN,
                               peer_id=peer, text=text))
        if chosen is None:
            print("未发送。")
            continue
        if peer:
            send_chosen(cfg, peer, chosen)
        else:
            print(f"（未设置 peer，仅展示）{chosen}")


def cmd_send(args, cfg: Config) -> int:
    peer = args.peer or cfg.peer
    if not peer:
        print("需要 --peer 或 config.yaml 的 peer。", file=sys.stderr)
        return 2
    return 0 if send_chosen(cfg, peer, args.text) else 1


def cmd_memory(args, cfg: Config) -> int:
    """查看与管理长期记忆。"""
    from .memory import MemoryStore

    if not cfg.memory.enabled:
        print("长期记忆已在配置里关闭（memory.enabled）")
        return 1

    store = MemoryStore(cfg.memory.db_path)
    try:
        # 清空某个对象的记忆
        if args.forget:
            info = store.forget(args.forget)
            print(f"已清空 {args.forget} 的记忆："
                  f"{info['messages']} 条消息 / {info['facts']} 条事实")
            return 0

        # 没指定对象：总览
        if not args.peer:
            st = store.stats()
            print("=" * 62)
            print("长期记忆总览")
            print("=" * 62)
            print(f"  数据库  : {st['db']}")
            print(f"  消息总数: {st['messages']}")
            print(f"  事实总数: {st['facts']}")
            print(f"  体积    : {st['size_kb']} KB")
            if st["earliest"]:
                print(f"  时间跨度: {_fmt_ts(st['earliest'])} ~ {_fmt_ts(st['latest'])}")
            peers = store.peers()
            if peers:
                print("\n  会话对象：")
                for p in peers:
                    print(f"    {p['peer_id']:<14} {p['messages']:>6} 条   "
                          f"最近 {_fmt_ts(p['last'])}")
            faces = store.face_names()
            if faces:
                print(f"\n  自学习的表情名: {len(faces)} 个")
            return 0

        # 指定对象
        st = store.stats(args.peer)
        print("=" * 62)
        print(f"长期记忆 · {args.peer}")
        print("=" * 62)
        print(f"  消息 {st['messages']} 条 / 事实 {st['facts']} 条")

        if args.search:
            print(f"\n--- 检索 {args.search!r} ---")
            hits = store.recall(args.peer, args.search, limit=args.limit)
            if hits:
                for m in hits:
                    print("  " + m.line())
            else:
                print("  （没有匹配的旧消息）")
            fhits = store.recall_facts(args.peer, args.search, limit=args.limit)
            if fhits:
                print("  相关事实：")
                for f in fhits:
                    print("    " + f.line())
            return 0

        if args.facts:
            facts = store.facts(args.peer, limit=args.limit)
            print(f"\n--- 长期事实（{len(facts)} 条） ---")
            for f in facts:
                print(f"  [{f.kind}] {f.content}")
            if not facts:
                print("  （还没有抽取到事实，多聊几轮就会自动积累）")
            return 0

        # 默认：最近对话
        rows = store.recent(args.peer, args.limit)
        print(f"\n--- 最近 {len(rows)} 条 ---")
        for m in rows:
            print("  " + m.line())
        return 0
    finally:
        store.close()


def _fmt_ts(ts: Optional[float]) -> str:
    import time as _t
    return _t.strftime("%Y-%m-%d %H:%M", _t.localtime(ts)) if ts else "-"


def cmd_serve_ws(args, cfg: Config) -> int:
    """启动 OneBot WebSocket 服务（连接 NapCat 的 WS 服务器）。"""
    from .service_ws import run_ws

    if args.peer:
        cfg.bot.target_peer = args.peer
    if args.owner:
        cfg.bot.owner = args.owner
    if args.ws_url:
        cfg.bot.ws_url = args.ws_url
    return run_ws(cfg, url=cfg.bot.ws_url)


def cmd_serve(args, cfg: Config) -> int:
    """启动 OneBot 事件服务（NapCat webhook）。"""
    from .service import run

    if args.port:
        cfg.bot.port = args.port
    if args.host:
        cfg.bot.host = args.host
    if args.peer:
        cfg.bot.target_peer = args.peer
    if args.owner:
        cfg.bot.owner = args.owner
    return run(cfg)


def cmd_check(args, cfg: Config) -> int:
    print("=== 配置 ===")
    print(json.dumps(cfg.to_dict(), ensure_ascii=False, indent=2))

    print("\n=== 推理后端 ===")
    try:
        client = build_client(cfg.llm)
        print(f"客户端：{type(client).__name__}（backend={cfg.llm.backend}）")
        resp = client.chat_json(
            "你是一个测试助手，只输出 JSON。",
            '请输出 {"ok": true, "note": "中文测试"} 这个 JSON。')
        print(f"连通性：OK，返回 {json.dumps(resp, ensure_ascii=False)[:120]}")
    except Exception as e:
        print(f"连通性：失败 —— {e}")
        print("提示：backend=echo 可离线跑通；local_ollama 需先 `ollama serve`；"
              "openai_compat 需填 base_url 与 api_key。")
        return 1

    print("\n=== 发送适配器 ===")
    try:
        sender = make_sender(cfg)
        print(f"适配器：{sender.name}")
        info = getattr(sender, "get_login_info", None)
        if callable(info):
            print(f"登录信息：{info()}")
    except Exception as e:
        print(f"适配器不可用：{e}")
    return 0


def cmd_style(args, cfg: Config) -> int:
    from .corpus import analyze_style
    from .style import write_style_report

    msgs = read_messages(args.corpus)
    mine = [m for m in msgs if m.direction.is_mine]
    stats = analyze_style(mine)
    guide = build_style_guide_raw(stats)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(guide, encoding="utf-8")
    stats_path = out.with_suffix(".json")
    stats_path.write_text(json.dumps(stats, ensure_ascii=False, indent=2),
                          encoding="utf-8")
    report = out.with_name("style_report.md")
    try:
        write_style_report(stats, report)
    except Exception as e:
        print(f"[warn] 报告生成失败：{e}", file=sys.stderr)
    print(f"✓ 风格指南：{out}\n✓ 统计：{stats_path}\n✓ 报告：{report}")
    print(f"  样本 {len(mine)} 条，平均 {stats.get('avg_chars')} 字")
    return 0


# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="luxun-qq",
        description="QQ 自动回复助手：三步思考 + 鲁迅式锐评 + 三候选选择发送")
    p.add_argument("--config", default=None, help="配置文件路径（默认 config.yaml）")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("plan", help="为一条对方消息生成三步思考与三个候选")
    sp.add_argument("text", nargs="?", default="", help="对方的消息")
    sp.add_argument("--file", help="从文件读取对方消息")
    sp.add_argument("--peer", help="对话对象 QQ 号")
    sp.add_argument("--send", action="store_true", help="交互选择后发送")
    sp.add_argument("--json", action="store_true", help="额外打印 JSON")
    sp.add_argument("--fallback", action="store_true", help="模型失败时打印降级方案")
    sp.set_defaults(func=cmd_plan)

    sc = sub.add_parser("chat", help="交互式连续对话")
    sc.add_argument("--peer", help="对话对象 QQ 号")
    sc.set_defaults(func=cmd_chat)

    ss = sub.add_parser("send", help="直接发送一条文本（不经过模型）")
    ss.add_argument("--peer", help="对话对象 QQ 号")
    ss.add_argument("--text", required=True)
    ss.set_defaults(func=cmd_send)

    sk = sub.add_parser("check", help="自检后端与发送适配器")
    sk.set_defaults(func=cmd_check)

    sv = sub.add_parser("serve", help="启动 OneBot 事件服务（HTTP webhook 模式）")
    sv.add_argument("--host", help="监听地址")
    sv.add_argument("--port", type=int, help="监听端口")
    sv.add_argument("--peer", help="要回复的对象 QQ 号")
    sv.add_argument("--owner", help="审批人 QQ 号（通常是登录 NapCat 的号）")
    sv.set_defaults(func=cmd_serve)

    sw = sub.add_parser("serve-ws",
                        help="启动 OneBot WebSocket 服务（连接 NapCat 的 WS 服务器，推荐）")
    sw.add_argument("--ws-url", help="NapCat 的 WebSocket 地址，如 ws://127.0.0.1:3001")
    sw.add_argument("--peer", help="要回复的对象 QQ 号")
    sw.add_argument("--owner", help="审批人 QQ 号")
    sw.set_defaults(func=cmd_serve_ws)

    mem = sub.add_parser("memory", help="查看与管理长期记忆")
    mem.add_argument("--peer", help="只看这个对象的记忆；省略则显示总览")
    mem.add_argument("--search", help="在该对象的记忆里检索关键词")
    mem.add_argument("--facts", action="store_true", help="列出长期事实")
    mem.add_argument("--forget", metavar="QQ", help="清空指定对象的全部记忆")
    mem.add_argument("--limit", type=int, default=20, help="显示条数（默认 20）")
    mem.set_defaults(func=cmd_memory)

    st = sub.add_parser("style", help="从导出语料生成风格指南")
    st.add_argument("--corpus", required=True, help="归一化消息 JSONL")
    st.add_argument("--out", default=str(CORPUS_DIR / "style_guide.md"))
    st.set_defaults(func=cmd_style)
    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = Config.load(args.config)
    try:
        return args.func(args, cfg) or 0
    except KeyboardInterrupt:
        print("\n已中断。")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
