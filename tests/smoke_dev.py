# -*- coding: utf-8 -*-
"""开发期冒烟自检：不依赖模型、不依赖网络、不依赖真实聊天数据。

与 tests/test_luxun_qq.py 的分工：
  - test_luxun_qq.py  ：单元测试（加解密、解析、适配器）
  - 本文件            ：端到端冒烟，走完整「对方消息 -> 三步思考 -> 三候选 -> 选择 -> 发送」

运行：
    python tests/smoke_dev.py
"""
from __future__ import annotations

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from luxun_qq.config import Config                      # noqa: E402
from luxun_qq.engine import ReplyEngine                 # noqa: E402
from luxun_qq.llm import EchoClient                     # noqa: E402
from luxun_qq.persona import LUXUN_PERSONA, load_style_sample  # noqa: E402
from luxun_qq.senders import build_sender               # noqa: E402

SCENES = [
    "今天又加班到这个点，你睡了吗",
    "在吗？借我两千块钱急用",
    "我最近好烦，感觉做什么都没意思",
    "哈哈哈哈你看这个也太离谱了吧",
    "你昨天怎么不回我消息",
]


def main() -> int:
    outbox = os.path.join(ROOT, "tests", "_tmp_run", "outbox")
    os.makedirs(outbox, exist_ok=True)

    cfg = Config.from_dict({"llm": {"backend": "echo"}})
    cfg.retrieve.enabled = False

    print("=" * 72)
    print("luxun-qq 端到端冒烟")
    print("=" * 72)
    print(f"人格设定长度：{len(LUXUN_PERSONA)} 字符")
    sample = load_style_sample(cfg.retrieve.style_sample)
    print(f"风格样本：{'已加载 ' + str(len(sample)) + ' 字符' if sample else '（无）'}")

    client = EchoClient(cfg.llm)
    engine = ReplyEngine(cfg, client=client, style_guide=sample, persona=LUXUN_PERSONA)
    sender = build_sender(cfg.sender, outbox)

    failures: list[str] = []
    for i, text in enumerate(SCENES, 1):
        print(f"\n{'-' * 72}\n场景 {i}/{len(SCENES)}：{text}\n{'-' * 72}")
        try:
            plan = engine.plan(text, peer_id="1987654321")
        except Exception as e:
            failures.append(f"场景{i} 生成失败：{e}")
            print(f"  ✗ 生成失败：{e}")
            continue

        # 三步思考必须齐全
        for name, value in (("step1_read.literal", plan.step1.literal),
                            ("step1_read.intent", plan.step1.intent),
                            ("step2_emotion.emotion", plan.step2.emotion),
                            ("step2_emotion.need", plan.step2.need),
                            ("step3_strategy.strategy", plan.step3.strategy)):
            if not value:
                failures.append(f"场景{i} 缺少 {name}")

        cands = plan.candidates()
        if len(cands) != 3:
            failures.append(f"场景{i} 候选数不是 3（实际 {len(cands)}）")
        if len({c.text for c in cands}) != len(cands):
            failures.append(f"场景{i} 候选有重复")
        if any(not c.text.strip() for c in cands):
            failures.append(f"场景{i} 存在空候选")

        print(f"  读懂：{plan.step1.literal}")
        print(f"  意图：{plan.step1.intent}")
        print(f"  情绪：{plan.step2.emotion}（{plan.step2.intensity}）")
        print(f"  策略：{plan.step3.strategy}")
        for j, c in enumerate(cands, 1):
            tag = ["一锐", "一软", "一退"][j - 1] if j <= 3 else f"候选{j}"
            print(f"    [{j}] {tag}｜{c.text}")

        # 选第一个候选，走一次真实发送路径（dry_run 落盘）
        res = sender.send_candidates("1987654321", [c.text for c in cands], chosen=1)
        if not res.ok:
            failures.append(f"场景{i} 发送失败：{res.detail}")
        else:
            print(f"  ✓ 已发送（{res.adapter}）：{cands[0].text}")

    print("\n" + "=" * 72)
    files = [f for f in os.listdir(outbox) if f.endswith(".jsonl")]
    if files:
        path = os.path.join(outbox, files[0])
        lines = open(path, encoding="utf-8").read().splitlines()
        print(f"发送审计：{path}")
        print(f"  记录 {len(lines)} 条，最后一条：")
        print("  " + json.dumps(json.loads(lines[-1]), ensure_ascii=False)[:200])
        # 审计条数应不少于场景数
        if len(lines) < len(SCENES):
            failures.append(f"审计记录不足：{len(lines)} < {len(SCENES)}")
    else:
        failures.append("未产生发送审计文件")

    print("=" * 72)
    if failures:
        for f in failures:
            print(f"✗ {f}")
        print(f"\n✗ 冒烟失败：{len(failures)} 项")
        return 1
    print(f"✓ 冒烟通过：{len(SCENES)} 个场景全部走通三步思考 -> 三候选 -> 发送")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
