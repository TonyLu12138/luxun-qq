# -*- coding: utf-8 -*-
"""三步思考推理引擎。

流程：
  1) 读懂对方（literal / intent / facts）
  2) 分析情绪（emotion / intensity / need / risk）
  3) 给出回复策略（strategy / tone / avoid）+ 三个候选回复

三个候选之间要有真实差异（一锐、一软、一退），供人选择后再发送。
风格 = 我本人的聊天习惯（统计约束 + 检索到的真实历史） × 鲁迅式锐评腔调。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Optional

from .config import Config
from .llm import LLMClient, LLMError, build_client
from .schema import Exchange, Message
# ---------------------------------------------------------------- 输出结构

@dataclass
class Step1Read:
    literal: str = ""
    intent: str = ""
    facts: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"literal": self.literal, "intent": self.intent, "facts": self.facts}


@dataclass
class Step2Emotion:
    emotion: str = ""
    intensity: float = 0.0
    need: str = ""
    risk: str = ""

    def to_dict(self) -> dict:
        return {"emotion": self.emotion, "intensity": self.intensity,
                "need": self.need, "risk": self.risk}


@dataclass
class Candidate:
    text: str
    why: str = ""

    def to_dict(self) -> dict:
        return {"text": self.text, "why": self.why}


@dataclass
class Step3Strategy:
    strategy: str = ""
    tone: str = ""
    avoid: list[str] = field(default_factory=list)
    candidates: list[Candidate] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"strategy": self.strategy, "tone": self.tone,
                "avoid": self.avoid,
                "candidates": [c.to_dict() for c in self.candidates]}


@dataclass
class ReplyPlan:
    """一次完整的回复计划。"""

    step1: Step1Read
    step2: Step2Emotion
    step3: Step3Strategy
    incoming_text: str = ""
    used_examples: list[dict] = field(default_factory=list)
    raw: dict = field(default_factory=dict)
    degraded: bool = False          # 模型不可用等降级标记
    note: str = ""
    voice_score: float = 0.0        # 语气阶段自评分（0~1，越高越好）

    def to_dict(self) -> dict:
        return {
            "step1_read": self.step1.to_dict(),
            "step2_emotion": self.step2.to_dict(),
            "step3_strategy": self.step3.to_dict(),
            "incoming_text": self.incoming_text,
            "used_examples": self.used_examples,
            "degraded": self.degraded,
            "note": self.note,
        }

    def candidates(self) -> list[Candidate]:
        return self.step3.candidates

    def render(self) -> str:
        """终端可读渲染。"""
        L: list[str] = []
        L.append("┌─ 第一步：读懂对方 " + "─" * 40)
        L.append(f"│ 字面：{self.step1.literal}")
        L.append(f"│ 意图：{self.step1.intent}")
        if self.step1.facts:
            L.append(f"│ 事实：{'；'.join(self.step1.facts)}")
        L.append("├─ 第二步：分析情绪 " + "─" * 40)
        L.append(f"│ 情绪：{self.step2.emotion}（强度 {self.step2.intensity}）")
        L.append(f"│ 需求：{self.step2.need}")
        L.append(f"│ 雷区：{self.step2.risk}")
        L.append("├─ 第三步：回复策略 " + "─" * 40)
        L.append(f"│ 策略：{self.step3.strategy}")
        L.append(f"│ 腔调：{self.step3.tone}")
        if self.step3.avoid:
            L.append(f"│ 避免：{'；'.join(self.step3.avoid)}")
        L.append("└─ 候选回复 " + "─" * 44)
        for i, c in enumerate(self.step3.candidates, 1):
            L.append(f"  [{i}] {c.text}")
            if c.why:
                L.append(f"      ↳ {c.why}")
        if self.degraded:
            L.append(f"\n⚠ 降级模式：{self.note}")
        return "\n".join(L)


# ---------------------------------------------------------------- 引擎

class ReasoningError(RuntimeError):
    """三步思考无法产出可用结果。"""


def _coerce_float(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def parse_plan(data: dict, *, incoming_text: str = "") -> ReplyPlan:
    """把模型返回的 JSON 容错地转成 ReplyPlan。"""
    if not isinstance(data, dict):
        raise ReasoningError(f"模型返回不是 JSON 对象：{type(data).__name__}")

    s1 = data.get("step1_read") or {}
    s2 = data.get("step2_emotion") or {}
    s3 = data.get("step3_strategy") or {}

    facts = s1.get("facts") or []
    if isinstance(facts, str):
        facts = [facts]
    avoid = s3.get("avoid") or []
    if isinstance(avoid, str):
        avoid = [avoid]

    cands: list[Candidate] = []
    for c in (s3.get("candidates") or []):
        if isinstance(c, str):
            cands.append(Candidate(text=c))
        elif isinstance(c, dict):
            txt = c.get("text") or c.get("reply") or ""
            if txt:
                cands.append(Candidate(text=txt, why=c.get("why") or ""))

    if not cands:
        raise ReasoningError("模型未返回任何候选回复")

    return ReplyPlan(
        step1=Step1Read(
            literal=str(s1.get("literal") or ""),
            intent=str(s1.get("intent") or ""),
            facts=[str(x) for x in facts][:6],
        ),
        step2=Step2Emotion(
            emotion=str(s2.get("emotion") or ""),
            intensity=_coerce_float(s2.get("intensity")),
            need=str(s2.get("need") or ""),
            risk=str(s2.get("risk") or ""),
        ),
        step3=Step3Strategy(
            strategy=str(s3.get("strategy") or ""),
            tone=str(s3.get("tone") or ""),
            avoid=[str(x) for x in avoid][:6],
            candidates=cands[:4],
        ),
        incoming_text=incoming_text,
    )


class ReplyEngine:
    """核心引擎：上下文 + 风格 -> 三步思考 + 三候选。"""

    def __init__(self, cfg: Config, *, client: Optional[LLMClient] = None,
                 retriever: Any = None, style_guide: str = "",
                 persona: Any = None):
        self.cfg = cfg
        self.client = client or build_client(cfg.llm)
        self.retriever = retriever
        self.style_guide = style_guide
        # persona 可以是 Persona 对象、人格 id 字符串，或 None（按配置选）
        from .personas import Persona, get_persona

        if isinstance(persona, Persona):
            self.persona_obj = persona
        elif isinstance(persona, str) and persona.strip():
            self.persona_obj = get_persona(persona)
        else:
            self.persona_obj = get_persona(getattr(cfg, "persona", None))
        # 兼容旧字段：engine.persona 仍是提示词字符串
        self.persona = self.persona_obj.prompt
        self._last_note = ""

    # ---------------------------------------------------------- prompt 组装
    def candidate_labels(self) -> list[str]:
        """当前人格的三个候选名称（用于展示）。"""
        return self.persona_obj.candidate_labels()

    def _system_prompt(self) -> str:
        """单段式用的人格提示词（含候选分工与硬性要求）。"""
        from .fewshot import FEWSHOT_BLOCK

        p = self.persona_obj
        parts = [p.prompt]
        if self.style_guide and p.use_style_sample:
            parts.append("\n## 我的说话习惯（实测，优先遵守）\n" + self.style_guide)
        if p.plan_text():
            parts.append("\n## 三个候选的分工\n" + p.plan_text())
        # 锐评腔的 few-shot 只对 luxun 人格有用，情感大师走自己的腔调
        if p.id == "luxun":
            parts.append("\n" + FEWSHOT_BLOCK)
        parts.append("\n## 硬性要求（在此重申）\n"
                     "1. 三个候选必须策略上真有不同取舍，不允许同义改写。\n"
                     "2. 用词、句长、标点习惯要像真人在微信上打字，不要书面语、不要客服腔。\n"
                     "3. 分清对话方向：对方讲的是他自己的事，不要反问、不要张冠李戴。\n"
                     "4. intensity 必须是 0 到 1 之间的小数（如 0.7），不要写 3 或 70。\n"
                     "5. 每个候选 1-3 句；需要拆句时用换行分隔。\n"
                     "6. 只输出一个 JSON 对象，不要代码块、不要额外文字。")
        for i, rule in enumerate(p.extra_rules, 1):
            parts.append(f"{i + 6}. {rule}")
        return "\n".join(parts)

    def _voice_system(self) -> str:
        """语气阶段用的 system prompt（按人格切换）。"""
        from .fewshot import FEWSHOT_BLOCK, VOICE_SYSTEM

        p = self.persona_obj
        if p.id == "luxun":
            sys_p = VOICE_SYSTEM
        else:
            # 情感大师：语气阶段自带人格描述与候选分工，不掺锐评示例
            sys_p = (
                "你替「我」写聊天回复。下面是我是谁、以及该怎么说话：\n\n"
                + p.prompt
                + "\n\n## 你要输出的三个候选\n"
                + (p.plan_text() or
                   "- 候选1：情商线\n- 候选2：直球线\n- 候选3：稳一手")
                + "\n\n## 输出格式（严格遵守）\n"
                "只输出 3 行，每行以「1||」「2||」「3||」开头，后面直接跟候选内容。\n"
                "不要解释、不要编号以外的文字、不要引号、不要 markdown。\n"
                "每条候选 1-3 句，像真人在微信上打出来的，不要写成小作文。\n"
            )
            for rule in p.extra_rules:
                sys_p += f"- {rule}\n"
            if p.self_check:
                sys_p += ("\n## 写完必须自查（不满足就重写）\n"
                          + "\n".join(f"- {c}" for c in p.self_check) + "\n")
        proof = ("\n## 写完必须自查（不满足就重写）\n"
                 "1. 必须是**恰好 3 行**，一行都不能少。只写 1 条或 2 条视为失败。\n"
                 "2. 三行都要有内容，每行至少 6 个字，不许空、不许只写「嗯」。\n")
        if p.id == "luxun":
            proof += ("3. 第 1 条要锋利（含反转或归谬），第 2 条留余地，第 3 条极短。\n"
                      "4. 不许出现「早点休息/辛苦了/注意身体/加油」这类客套。\n")
        else:
            proof += ("3. 第 1 条既接住情绪又给确定性，第 2 条把想法说明白，"
                      "第 3 条短但不许冷落对方。\n"
                      "4. 全部用中文，不许出现英文单词。\n")
        sys_p += proof
        for c in p.self_check:
            sys_p += f"- {c}\n"
        if p.id == "luxun" and p.use_style_sample:
            pass
        return sys_p

    def _retrieve_block(self, incoming_text: str) -> tuple[str, list[dict]]:
        if not (self.cfg.retrieve.enabled and self.retriever):
            return "", []
        try:
            hits = self.retriever.search(
                incoming_text,
                top_k=self.cfg.retrieve.top_k,
                min_score=self.cfg.retrieve.min_score)
        except Exception:
            return "", []
        if not hits:
            return "", []
        lines = ["\n## 我以前遇到类似话时，真实是这么回的（模仿这种语感，不要照抄）"]
        for h in hits:
            inc = (h.get("incoming_text") or "").strip()
            rep = (h.get("reply_text") or "").strip()
            if not inc or not rep:
                continue
            lines.append(f"对方：{inc}\n我：{rep}")
        return "\n".join(lines), hits

    def _user_prompt(self, incoming_text: str, history: list[Message] | None = None,
                     peer_id: str = "") -> str:
        lines: list[str] = []
        if history:
            lines.append("## 最近的对话上下文")
            for m in history[-20:]:
                who = "我" if m.direction.is_mine else "对方"
                body = m.text or f"[{m.kind.value}]"
                lines.append(f"{who}：{body}")
            lines.append("")
        if peer_id:
            lines.append(f"（对话对象 QQ：{peer_id}）\n")
        lines.append("## 对方的消息（下方标签内是原文，需要你回复的就是它）")
        lines.append(f"<对方消息>{incoming_text}</对方消息>")
        lines.append("\n请按输出协议返回 JSON。")
        return "\n".join(lines)

    # ---------------------------------------------------------- 调用
    def plan(self, incoming_text: str, *, history: list[Message] | None = None,
             peer_id: str = "", retries: int = 2,
             memory: dict[str, Any] | None = None) -> ReplyPlan:
        """生成回复计划。

        默认走**两段式**：先让模型自由写三条候选（语气优先），再单独一次调用
        把它结构化。原因（实测）：7B 级模型在"一次调用同时填三步思考 JSON +
        写候选"时会把注意力全用在结构上，语气会塌成客服腔；
        拆开后语气质量明显更好。

        Args:
            memory: 长期记忆上下文，形如
                ``{"old_messages": [MemoryMessage...], "facts": [MemoryFact...]}``
                由 ``BotService.build_memory_context`` 组装。
        """
        if not incoming_text.strip():
            raise ReasoningError("对方消息为空，无法规划回复")

        if self.cfg.llm.two_stage:
            try:
                return self._pad_candidates(self._plan_two_stage(
                    incoming_text, history, peer_id, retries, memory))
            except ReasoningError as e:
                # 两段式失败时退回单段式，保证仍能出结果
                self._last_note = f"两段式失败已回退：{e}"
        return self._pad_candidates(self._plan_one_stage(
            incoming_text, history, peer_id, retries, memory))

    @staticmethod
    def _pad_candidates(plan: ReplyPlan) -> ReplyPlan:
        """保证候选至少有 3 条。

        为什么需要：两段式全部重试都凑不齐 3 条时会回退到单段式，而单段式
        拿到的 JSON 可能只带了 1-2 个候选。UI 上「三个候选供你选」是核心
        体验，少于 3 条会让选择失去意义。这里用现有候补位，并在 why 里
        说明是模型没给够，既不假装是模型写的，也不让用户面对残缺列表。
        """
        if len(plan.step3.candidates) >= 3:
            return plan
        have = list(plan.step3.candidates)
        if not have:
            raise ReasoningError("模型没有给出任何候选")
        tags = ["候选2", "候选3", "候选4"]
        idx = 0
        while len(plan.step3.candidates) < 3:
            src = have[min(idx, len(have) - 1)]
            plan.step3.candidates.append(Candidate(
                text=src.text,
                why="（模型本次没给够三条，这条是补齐位，建议重新生成）"))
            idx += 1
        plan.note = (plan.note + "；" if plan.note else "") + \
            f"模型只给出 {len(have)} 条候选，已补齐到 3 条"
        return plan

    # ---------------------------------------------------------- 记忆片段
    def _memory_block(self, memory: dict[str, Any] | None) -> str:
        """把长期记忆渲染成提示词片段。

        实测踩过的坑（这段里的"反例"都是真实出现过的错误）：
            记忆放在 system 段，而人格提示词是以"我"的口吻写的，
            结果模型把「关于对方的事实」当成了自己的事——
            对方有面试，它回「我准备得差不多了，你呢？」。
            所以这里必须**反复强调归属**，并给出具体反例。
        """
        if not memory:
            return ""
        facts = memory.get("facts") or []
        old = memory.get("old_messages") or []
        if not facts and not old:
            return ""

        parts: list[str] = [
            "\n## 你记得的、关于【对方】的事",
            "（下面每一条讲的都是【对方】，不是你。你是回复者。）",
        ]
        if facts:
            parts.append("\n对方的基本情况：")
            for f in facts[:24]:
                line = f.line() if hasattr(f, "line") else str(f)
                parts.append("  - " + line)
        if old:
            parts.append("\n更早之前对方说过的话：")
            for m in old[:8]:
                line = m.line() if hasattr(m, "line") else str(m)
                parts.append("  " + line)

        parts.append(
            "\n使用要求（违反即失败）：\n"
            "1. **归属不能错**。上面都是对方的事，别当成你自己的。\n"
            "   反例：对方有面试，你回「我准备得差不多了，你呢？」——错，\n"
            "        应该说「面试准备得怎么样了」。\n"
            "2. **只在直接相关时才用**。对方当前的话跟某条记忆沾边，\n"
            "   就自然带一句；**不相关就完全别提**。\n"
            "   反例：对方说「吃火锅」，你回「听说你下周面试」——硬塞，错。\n"
            "3. **不要生硬复述**。别写「你上次说过…」这种查户口的话。\n"
            "4. **只用上面列出的信息，不要推测任何额外细节**。\n"
            "   反例：只写了「养了只猫」，就别说猫吃了什么、什么颜色。\n"
            "5. **不要主动重提旧话题**。上面提到的面试、猫这些事，\n"
            "   只在对方这句话里提到了才回应；对方没提就别扯出来。\n"
            "   反例：对方说「今天好累」，你回「听说下周面试」——硬塞，错。\n"
            "6. 记忆可能过时，与当前消息冲突时**以当前消息为准**。\n"
            "7. 三条候选都要能直接发出去，像真人在微信上打的。\n"
        )
        return "\n".join(parts)

    # ---------------------------------------------------------- 重写提示
    def _regen_hint(self, issues: list[str]) -> str:
        """根据人格与具体问题，生成"带批评的重新生成"提示。

        这一步很关键：7B 模型第一次常常写得平庸，给出**具体问题 + 该人格的
        自查清单**后，第二次质量通常明显提升。
        """
        p = self.persona_obj
        head = "上次的问题：" + "；".join(issues) + "。请重写三条。"
        if p.id == "luxun":
            body = ("第 1 条要锋利、含一次反转或归谬；第 2 条清醒但留余地；"
                    "第 3 条极短。禁止「早点休息/辛苦了/注意身体/加油/多喝热水」"
                    "这类客套。")
        else:
            labels = p.candidate_labels()
            body = (f"第 1 条（{labels[0]}）要既接住情绪又给确定性；"
                    f"第 2 条（{labels[1]}）直接把我的想法说明白；"
                    f"第 3 条（{labels[2]}）要短，但不许冷落对方。")
        rules = ""
        if p.self_check:
            rules = "\n必须满足：\n" + "\n".join(f"- {c}" for c in p.self_check) + "\n"
        return (head + body + rules +
                "仍然按 1||2||3|| 三行输出，只输出这三行。")

    # ---------------------------------------------------------- 两段式
    def _plan_two_stage(self, incoming_text: str, history: list[Message] | None,
                        peer_id: str, retries: int,
                        memory: dict[str, Any] | None = None) -> ReplyPlan:
        from .fewshot import STRUCT_SYSTEM, parse_voice_output, score_candidates

        ex_block, hits = self._retrieve_block(incoming_text)
        ctx_lines: list[str] = []
        if history:
            ctx_lines.append("最近的对话：")
            for m in history[-10:]:
                who = "我" if m.direction.is_mine else "对方"
                ctx_lines.append(f"  {who}：{m.text or f'[{m.kind.value}]'}")
        # 长期记忆放在 **user** 段，作为"背景资料"，而不是放进 system。
        # 实测：放进 system 时，模型会把"关于对方的事实"和人格提示词里的
        # "我"混起来，答成"我准备得差不多了"这种视角错乱。
        # 放在 user 段并明确标成背景资料，归属就清楚多了（标准 RAG 做法）。
        mem_block = self._memory_block(memory)
        voice_sys = self._voice_system()
        # 注意：语气阶段**不拼 style_guide**。实测把风格量化约束混进来会让模型
        # 分心去凑指标，语气立刻塌成客服腔；这些约束放到结构化阶段更合适。
        voice_sys += ex_block

        voice_user = "\n".join(ctx_lines + ([mem_block, ""] if mem_block else []) + [
            f"对方（{peer_id or '朋友'}）发来的消息：{incoming_text}",
            "",
            "请写 3 条回复，按 1||2||3|| 三行输出。",
        ])

        # 语气阶段是质量瓶颈，且 7B 模型在高温下方差很大。
        # 因此做「生成 -> 严格判分 -> 不合格就带批评重生成」，取分数最高的一次。
        # 注意：每次重生成都必须重新带上完整上下文（原消息），否则模型会越试越跑偏。
        attempts = max(1, int(self.cfg.llm.voice_attempts))
        best: tuple[float, list[str], str] | None = None
        last_err: Optional[Exception] = None
        crit = ""

        for a in range(attempts):
            user_a = voice_user + (("\n\n【上次不合格，必须修正】\n" + crit)
                                   if crit else "")
            try:
                raw = self.client.chat_text(voice_sys, user_a,
                                            temperature=self.cfg.llm.temperature,
                                            max_tokens=300)
                cands = parse_voice_output(raw)
                if len(cands) < 3:
                    crit = ("上次只给出了 %d 条，不合格。**必须恰好 3 行**，"
                            "每行以「1||」「2||」「3||」开头，每行都要有内容"
                            "（至少 6 个字）。不要输出解释、不要复述对方的话、"
                            "不要用中文编号。" % len(cands))
                    last_err = ReasoningError(f"只解析出 {len(cands)} 条")
                    continue
                # 三条里若有空/过短的，同样判为不合格（否则会出现"1 条候选"的情况）
                too_short = [i for i, c in enumerate(cands, 1)
                             if len(c.strip()) < 4]
                if too_short:
                    crit = ("第 %s 条太短或为空。三条都要是能直接发出去的完整句子，"
                            "每行至少 6 个字。请重写。" %
                            "、".join(map(str, too_short)))
                    last_err = ReasoningError(f"第 {too_short} 条过短")
                    continue
                sc, issues = score_candidates(cands, incoming_text, self.persona_obj)
                if best is None or sc > best[0]:
                    best = (sc, cands, raw)
                if sc >= 0.85:
                    break                      # 够好了，不再浪费算力
                crit = self._regen_hint(issues)
                self._last_note = "语气重试：" + "；".join(issues)
            except LLMError as e:
                last_err = e
                continue

        if best is None:
            raise ReasoningError(f"语气阶段失败：{last_err}")

        score, cands, raw = best
        numbered = "\n".join(f"{i}||{c}" for i, c in enumerate(cands, 1))
        struct_user = (
            f"对方发来的消息：{incoming_text}\n\n"
            f"已有的三条候选（必须逐字保留，只加 why 说明）：\n{numbered}\n\n"
            f"请按要求输出 JSON。"
        )
        try:
            data = self.client.chat_json(
                STRUCT_SYSTEM + (("\n\n" + self.style_guide)
                                 if self.style_guide else ""),
                struct_user,
                max_tokens=max(self.cfg.llm.max_tokens, 1200))
        except (LLMError, ReasoningError) as e:
            raise ReasoningError(f"结构化阶段失败：{e}") from e

        plan = parse_plan(data, incoming_text=incoming_text)
        plan.used_examples = hits
        plan.voice_score = score
        plan.raw = {"voice": raw, "struct": data, "two_stage": True,
                    "voice_score": score}
        return plan

    # ---------------------------------------------------------- 单段式
    def _plan_one_stage(self, incoming_text: str, history: list[Message] | None,
                        peer_id: str, retries: int,
                        memory: dict[str, Any] | None = None) -> ReplyPlan:
        ex_block, hits = self._retrieve_block(incoming_text)
        system = self._system_prompt() + ex_block
        user = self._user_prompt(incoming_text, history, peer_id)
        # 与两段式一致：记忆作为背景资料放在 user 段
        mem_block = self._memory_block(memory)
        if mem_block:
            user = mem_block + "\n\n" + user

        last_err: Optional[Exception] = None
        for attempt in range(retries + 1):
            try:
                data = self.client.chat_json(system, user)
                plan = parse_plan(data, incoming_text=incoming_text)
                plan.used_examples = hits
                plan.raw = data
                return plan
            except (LLMError, ReasoningError) as e:
                last_err = e
                if attempt < retries:
                    user = user + "\n\n注意：上一次输出无法解析。只输出一个 JSON 对象，" \
                                  "不要任何其他文字、不要代码块。"
                    continue
        raise ReasoningError(f"三步思考失败：{last_err}")

    def plan_from_exchange(self, exc: Exchange) -> ReplyPlan:
        incoming = "\n".join(m.text for m in exc.incoming if m.text)
        return self.plan(incoming, history=exc.incoming, peer_id=exc.peer_id)


# ---------------------------------------------------------------- 降级计划

def fallback_plan(incoming_text: str, note: str) -> ReplyPlan:
    """模型不可用时的降级计划：至少给出结构完整的三个占位候选。"""
    return ReplyPlan(
        step1=Step1Read(literal=incoming_text.strip(), intent="（模型不可用，未能推断）"),
        step2=Step2Emotion(emotion="未知", intensity=0.0,
                           need="（模型不可用）", risk="（模型不可用）"),
        step3=Step3Strategy(
            strategy="降级：未调用模型",
            tone="（无）",
            candidates=[
                Candidate(text="（候选1 待生成：模型不可用）", why=note),
                Candidate(text="（候选2 待生成：模型不可用）", why=note),
                Candidate(text="（候选3 待生成：模型不可用）", why=note),
            ],
        ),
        incoming_text=incoming_text,
        degraded=True,
        note=note,
    )
