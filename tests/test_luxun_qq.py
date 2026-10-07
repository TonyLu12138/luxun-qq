# -*- coding: utf-8 -*-
"""纯标准库测试集：不依赖 pytest、不依赖网络/模型/GPU。

运行方式：
    python tests/test_luxun_qq.py            # 自带 runner（推荐）
    python -m pytest tests -q                # 装了 pytest 也能跑

覆盖：
  1. SQLCipher 加解密往返（真实 SQLite → 加密 → 解密 → 逐页比对 + 完整性）
  2. 极简 protobuf 文本抽取
  3. JSON 抽取容错
  4. 三步思考的解析、降级、三候选差异
  5. 发送层适配器与审计落盘
  6. 风格样本加载
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import tempfile
import traceback

from Crypto.Cipher import AES

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from luxun_qq import config  # noqa: E402
from luxun_qq.config import (Config, LLMConfig, MemoryConfig,  # noqa: E402
                             SenderConfig)
from luxun_qq.engine import (ReasoningError, ReplyEngine, fallback_plan,  # noqa: E402
                             parse_plan)
from luxun_qq.llm import (EchoClient, OllamaClient, OpenAICompatClient,  # noqa: E402
                          build_client, extract_json_object)
from luxun_qq.persona import load_style_sample  # noqa: E402
from luxun_qq.qqdb import crypto  # noqa: E402
from luxun_qq.qqdb.export import extract_text  # noqa: E402
from luxun_qq.schema import Direction, Message  # noqa: E402
from luxun_qq.senders import build_sender  # noqa: E402
from luxun_qq.senders.base import SendError  # noqa: E402

PASS = b"Ab3xY9zQ1mN7pK2v"
PAGE = 4096
HEADER = 1024
ROWS = 60

_CLEANUPS: list[str] = []

# --------------------------------------------------------------------------- #
# 测试隔离：把记忆库的**默认路径**整体挪到临时目录
#
# MemoryConfig 的默认指向真实的 data/memory.db。任何测试只要构造 BotService
# 而没显式指定路径，就会打开甚至写入用户的真实长期记忆（踩过：测试数据被灌进
# 真实库）。逐个补 db_path 太脆弱、以后新写的测试还会漏，所以直接改默认值——
# MemoryConfig.__post_init__ 在实例化时读 config.DEFAULT_MEMORY_DB，
# 因此这一改对所有"未指定路径"的实例都生效。
# --------------------------------------------------------------------------- #
_TEST_MEM_DIR = os.path.join(ROOT, "tests", "_tmp_run")
os.makedirs(_TEST_MEM_DIR, exist_ok=True)
config.DEFAULT_MEMORY_DB = os.path.join(_TEST_MEM_DIR, "default_memory.db")


def _mem_db(name: str = "") -> str:
    """给需要互不干扰的测试一个**独立**的记忆库文件路径。

    默认路径已整体重定向到临时目录（见上），但多个 BotService 同时持有
    同一个库的连接仍会偶尔抢锁；对会真实读写的测试再给独立文件更稳。
    不主动删除（Windows 上连接未关时删不掉），交给结束时的 rmtree。
    """
    _SEQ[0] += 1
    suffix = f"_{name}" if name else ""
    return os.path.join(_TEST_MEM_DIR, f"mem{suffix}_{_SEQ[0]}.db")


# --------------------------------------------------------------------------- #
# 辅助
# --------------------------------------------------------------------------- #

def _varint(n: int) -> bytes:
    out = b""
    while True:
        b = n & 0x7F
        n >>= 7
        out += bytes([b | 0x80]) if n else bytes([b])
        if not n:
            return out


_SEQ = [0]


def tmpdir() -> str:
    """返回测试专用目录。

    注意：本环境（DSH 沙箱）不允许在新创建的子目录里继续创建文件，
    因此这里固定用一个已存在的目录，靠文件名区分，不再嵌套 mkdtemp。
    """
    d = os.path.join(ROOT, "tests", "_tmp_run")
    os.makedirs(d, exist_ok=True)
    _CLEANUPS.append(d)
    return d


def tmppath(name: str = "") -> str:
    """在测试目录里取一个唯一路径。"""
    _SEQ[0] += 1
    stem = name or f"f{_SEQ[0]}"
    return os.path.join(tmpdir(), f"{_SEQ[0]}_{stem}")


def make_plain_db(path: str) -> None:
    if os.path.exists(path):
        os.remove(path)
    con = sqlite3.connect(path)
    con.execute("create table msg(id integer primary key, peer text, text text, ts int)")
    con.executemany("insert into msg(peer,text,ts) values(?,?,?)",
                    [(f"p{i}", f"内容{i}" * 8, 1700000000 + i) for i in range(ROWS)])
    con.commit()
    con.close()
    # SQLCipher 按整页处理，测试夹具必须补齐到页的整数倍
    size = os.path.getsize(path)
    pad = (-size) % PAGE
    if pad:
        with open(path, "ab") as f:
            f.write(b"\x00" * pad)


def encrypt_like_sqlcipher(plain: str, out: str, passphrase: bytes, salt: bytes) -> None:
    """按 SQLCipher4 布局加密一个明文 SQLite 库（测试夹具）。

    文件结构：
        自定义头(1024) | 盐值(16) | 页1(4096) | 页2(4096) | ...
    页 1：页首 16 字节是明文 SQLite 头（不加密），密文区为其后 4000 字节；
    其余页：密文区为 4016 字节。页尾 80 字节为 IV+HMAC 保留区。
    """
    p = crypto.CipherParams(page_size=PAGE)
    key, hk = crypto.derive_keys(passphrase, salt, p)
    data = open(plain, "rb").read()
    assert len(data) % PAGE == 0, len(data)

    with open(out, "wb") as f:
        f.write(b"\x00" * HEADER)     # NTQQ 自定义头
        f.write(salt)                 # 盐值（密钥派生用）
        for i in range(len(data) // PAGE):
            pt = data[i * PAGE:(i + 1) * PAGE]
            if i == 0:
                assert pt[:16] == b"SQLite format 3\x00", pt[:16]
                ct_len = PAGE - 16 - p.reserve        # 4000
                body = pt[16:16 + ct_len]             # 加密 [16, 4016)
                head16 = pt[:16]                      # 明文头单独写
            else:
                ct_len = PAGE - p.reserve             # 4016
                body = pt[:ct_len]
                head16 = b""
            body = body.ljust(ct_len, b"\x00")
            iv = bytes((i + k) % 256 for k in range(16))
            ct = AES.new(key, AES.MODE_CBC, iv).encrypt(body)
            payload = ct + iv
            page = head16 + payload + __import__("hmac").new(hk, payload, "sha512").digest()
            assert len(page) == PAGE, len(page)
            f.write(page)


def make_cipher_files() -> tuple[str, str]:
    plain = tmppath("plain.db")
    enc = tmppath("enc.db")
    make_plain_db(plain)
    encrypt_like_sqlcipher(plain, enc, PASS, bytes(range(16)))
    return plain, enc


# --------------------------------------------------------------------------- #
# 1. SQLCipher
# --------------------------------------------------------------------------- #

def test_verify_key_accepts_correct_rejects_wrong() -> None:
    _, enc = make_cipher_files()
    p = crypto.CipherParams(page_size=PAGE)
    assert crypto.verify_key(enc, PASS.decode(), p) is True
    assert crypto.verify_key(enc, "wrongkey12345678", p) is False


def test_decrypt_roundtrip_preserves_data() -> None:
    plain, enc = make_cipher_files()
    dec = tmppath("dec.db")
    p = crypto.CipherParams(page_size=PAGE)
    stats = crypto.decrypt_to_plaintext(enc, dec, PASS.decode(), p, progress_every=0)
    assert stats["bad_pages"] == 0, stats
    assert stats["pages"] > 0

    a = open(plain, "rb").read()
    b = open(dec, "rb").read()
    assert len(a) == len(b), (len(a), len(b))

    # 每页的明文头 + 保留区不参与本实现的往返（见 README「已知限制」）：
    #   页 1  : 头 16 字节 + 尾 96 字节（16 头位 + 80 IV/HMAC 位）
    #   其余页: 尾 80 字节
    # 中间全部内容必须逐字节一致——这是数据库真正承载数据的区间。
    for i in range(len(a) // PAGE):
        off = i * PAGE
        if i == 0:
            lo, hi = 16, PAGE - 96
        else:
            lo, hi = 0, PAGE - p.reserve
        assert hi > lo, (lo, hi)
        assert a[off + lo:off + hi] == b[off + lo:off + hi], f"第 {i + 1} 页内容不一致"

    # 保留区丢失会破坏 SQLite 结构（页 1 尾部含 sqlite_master 记录），
    # 因此完整库的 integrity_check 会失败。这里显式断言这一已知限制，
    # 避免把问题藏起来：探针库/含索引的库必须走 sqlcipher3 原生产物。
    try:
        con = sqlite3.connect(dec)
        result = con.execute("pragma integrity_check").fetchone()[0]
        con.close()
        structurally_ok = (result == "ok")
    except sqlite3.DatabaseError:
        structurally_ok = False
    assert structurally_ok is False, (
        "若此断言开始失败，说明保留区已被正确保留，应把本测试升级为完整 integrity_check")


def test_decrypt_rejects_wrong_key() -> None:
    _, enc = make_cipher_files()
    out = tmppath("x.db")
    try:
        crypto.decrypt_to_plaintext(enc, out, "wrongkey12345678",
                                    crypto.CipherParams(page_size=PAGE),
                                    progress_every=0)
    except crypto.DecryptError:
        return
    raise AssertionError("错误密钥未被拒绝")


def test_cipher_params_layout() -> None:
    p = crypto.CipherParams(page_size=PAGE)
    assert p.reserve == 80, p.reserve
    assert p.payload_size == PAGE - 16 - p.reserve == 4000, p.payload_size
    assert p.hmac_size == 64


# --------------------------------------------------------------------------- #
# 2. protobuf / schema
# --------------------------------------------------------------------------- #

def test_protobuf_text_extraction() -> None:
    text = "在吗".encode()
    inner = _varint((45101 << 3) | 2) + _varint(len(text)) + text
    assert extract_text(inner)[0] == "在吗"
    assert extract_text(None)[0] == ""
    assert extract_text(b"")[0] == ""
    assert extract_text(b"\xff\xff\xff")[0] == ""      # 脏数据不抛异常


def test_message_jsonl_roundtrip() -> None:
    m = Message(msg_id="1", ts=1700000000, direction=Direction.OUT,
                peer_id="123", text="测试")
    back = Message.from_dict(json.loads(m.to_jsonl()))
    assert back.text == "测试"
    assert back.direction is Direction.OUT
    assert back.kind is m.kind


# --------------------------------------------------------------------------- #
# 3. JSON 抽取容错
# --------------------------------------------------------------------------- #

def test_extract_json_object_variants() -> None:
    cases = [
        '{"a":1}',
        '```json\n{"a":1}\n```',
        '好的，这是结果：{"a":1} 希望有帮助',
        '{"a":"含 } 的字符串"}',
        '前言 {"a":{"b":[1,2]}} 后记',
    ]
    for raw in cases:
        got = extract_json_object(raw)
        assert got is not None, raw
        assert json.loads(got)["a"] is not None, raw
    assert extract_json_object("完全不是 JSON") is None
    assert extract_json_object("") is None


# --------------------------------------------------------------------------- #
# 4. 三步思考
# --------------------------------------------------------------------------- #

def _plan_dict() -> dict:
    return {
        "step1_read": {"literal": "字面", "intent": "意图", "facts": ["f1"]},
        "step2_emotion": {"emotion": "累", "intensity": 0.7, "need": "被理解",
                          "risk": "说教"},
        "step3_strategy": {"strategy": "先接情绪", "tone": "冷眼热心",
                           "avoid": ["说教"],
                           "candidates": [{"text": "a", "why": "w1"},
                                          {"text": "b", "why": "w2"},
                                          {"text": "c", "why": "w3"}]},
    }


def test_parse_plan_ok() -> None:
    plan = parse_plan(_plan_dict(), incoming_text="你好")
    assert plan.step2.intensity == 0.7
    assert len(plan.candidates()) == 3
    assert plan.candidates()[0].text == "a"
    assert plan.step1.facts == ["f1"]


def test_parse_plan_tolerates_loose_shapes() -> None:
    plan = parse_plan({
        "step1_read": {"literal": "x", "facts": "单个事实"},
        "step3_strategy": {"avoid": "不要说教", "candidates": ["只有文本"]},
    })
    assert plan.step1.facts == ["单个事实"]
    assert plan.step3.avoid == ["不要说教"]
    assert plan.candidates()[0].text == "只有文本"
    assert plan.step2.intensity == 0.0


def test_parse_plan_requires_candidates() -> None:
    try:
        parse_plan({"step1_read": {}, "step3_strategy": {"candidates": []}})
    except ReasoningError:
        return
    raise AssertionError("缺少候选时应当报错")


def test_engine_echo_produces_three_distinct_candidates() -> None:
    cfg = Config.from_dict({"llm": {"backend": "echo"}})
    cfg.retrieve.enabled = False
    engine = ReplyEngine(cfg, client=EchoClient(cfg.llm))
    plan = engine.plan("今天又加班到这个点，你睡了吗")

    assert len(plan.candidates()) == 3
    assert plan.step1.literal, "字面理解不能为空"
    assert "对方消息" not in plan.step1.literal, "不应把 prompt 说明当成消息原文"
    assert plan.step2.emotion
    assert plan.step3.strategy
    texts = {c.text for c in plan.candidates()}
    assert len(texts) == 3, "三个候选必须互不相同"
    assert all(c.why for c in plan.candidates())
    assert plan.render()  # 渲染不抛异常


def test_engine_literal_extraction_from_tagged_prompt() -> None:
    """engine 用 <对方消息> 包裹原文；echo 后端应只取出原文。"""
    cfg = Config.from_dict({"llm": {"backend": "echo"}})
    cfg.retrieve.enabled = False
    engine = ReplyEngine(cfg, client=EchoClient(cfg.llm))
    plan = engine.plan("在吗？借我两千块钱急用")
    assert plan.step1.literal == "在吗？借我两千块钱急用"


def test_fallback_plan_shape() -> None:
    plan = fallback_plan("你好", "模型不可用")
    assert plan.degraded
    assert len(plan.candidates()) == 3
    assert plan.note == "模型不可用"


def test_engine_reports_empty_message() -> None:
    cfg = Config.from_dict({"llm": {"backend": "echo"}})
    engine = ReplyEngine(cfg, client=EchoClient(cfg.llm))
    try:
        engine.plan("   ")
    except ReasoningError:
        return
    raise AssertionError("空消息应当报错")


def test_echo_scene_routing() -> None:
    """场景识别必须选到具体话题，而不是被「在吗/睡了吗」这类泛开场白抢走。"""
    cases = {
        "今天又加班到这个点，你睡了吗": "先接情绪再给台阶",
        "在吗？借我两千块钱急用": "把账说清，把人留住",
        "我最近好烦，感觉做什么都没意思": "先认下这份丧，再给出很小的动作",
        "哈哈哈哈你看这个也太离谱了吧": "先同频，再补一刀",
        "你昨天怎么不回我消息": "先认账，再给原因，不辩解",
    }
    client = EchoClient(LLMConfig(backend="echo"))
    for text, expect_strategy in cases.items():
        scene = client._match_scene(text)
        assert scene is not None, f"未匹配到场景：{text}"
        assert scene[4] == expect_strategy, (
            f"「{text}」期望策略 {expect_strategy}，实际 {scene[4]}")
        # 每个场景都必须给出恰好 3 个互不相同的候选
        cands = scene[5]
        assert len(cands) == 3, f"{text} 候选数 {len(cands)}"
        assert len({c[0] for c in cands}) == 3, f"{text} 候选重复"


def test_build_client_dispatch() -> None:
    assert isinstance(build_client(LLMConfig(backend="echo")), EchoClient)
    assert isinstance(build_client(LLMConfig(backend="local_ollama")), OllamaClient)
    assert isinstance(build_client(LLMConfig(backend="openai_compat")),
                      OpenAICompatClient)
    try:
        build_client(LLMConfig(backend="不存在"))
    except Exception:
        return
    raise AssertionError("未知 backend 应当报错")


# --------------------------------------------------------------------------- #
# 5. 风格样本
# --------------------------------------------------------------------------- #

def test_load_style_sample() -> None:
    p = tmppath("s.txt")
    with open(p, "w", encoding="utf-8") as f:
        f.write("行吧\n也行，随你\n")
    guide = load_style_sample(p)
    assert "行吧" in guide and "也行" in guide
    assert load_style_sample(tmppath("不存在.txt")) == ""
    assert load_style_sample(None) == ""


def test_load_style_sample_dialog_format() -> None:
    p = tmppath("d.txt")
    with open(p, "w", encoding="utf-8") as f:
        f.write("对方：在吗\n我：说\n")
    guide = load_style_sample(p)
    assert "对方：在吗" in guide and "我：说" in guide


# --------------------------------------------------------------------------- #
# 6. 发送层
# --------------------------------------------------------------------------- #

def test_dry_run_sender_writes_outbox() -> None:
    d = tmpdir()
    sender = build_sender(SenderConfig(adapter="dry_run"), d)
    res = sender.send_text("1987654321", "测试消息")
    assert res.ok and res.adapter == "dry_run"
    files = [f for f in os.listdir(d) if f.endswith(".jsonl")]
    assert files, "应当写出 outbox 审计文件"
    rec = json.loads(open(os.path.join(d, files[0]), encoding="utf-8")
                     .read().splitlines()[-1])
    assert rec["text"] == "测试消息"
    assert rec["peer_id"] == "1987654321"
    for key in ("ts", "adapter", "ok", "text", "peer_id"):
        assert key in rec, key


def test_send_candidates_choose_and_skip() -> None:
    d = tmpdir()
    sender = build_sender(SenderConfig(adapter="dry_run"), d)
    cands = ["甲", "乙", "丙"]

    skipped = sender.send_candidates("1", cands, chosen=None)
    assert skipped.ok, "只看不发也应记录成功"

    picked = sender.send_candidates("1", cands, chosen=1)
    assert picked.ok

    bad = sender.send_candidates("1", cands, chosen=9)
    assert bad.ok is False, "越界选择应当失败"


def test_unknown_sender_adapter_raises() -> None:
    try:
        build_sender(SenderConfig(adapter="不存在的适配器"), tmpdir())
    except SendError:
        return
    raise AssertionError("未知适配器应当抛 SendError")


def test_webhook_sender_fails_cleanly_without_server() -> None:
    """连不上时必须给出可读原因，且不能挂太久。

    webhook / napcat 都以 SendError 报告连接失败，调用方（CLI）统一捕获。
    """
    import time

    sender = build_sender(
        SenderConfig(adapter="webhook", base_url="http://127.0.0.1:9/none",
                     timeout=5), tmpdir())
    t0 = time.time()
    try:
        res = sender.send_text("1", "hi")
    except SendError as e:
        assert str(e), "失败时必须带原因"
        assert time.time() - t0 < 30, "不应挂太久"
        return
    assert res.ok is False
    assert res.detail


# --------------------------------------------------------------------------- #
# 6b. 自动回复服务（OneBot 事件状态机）
# --------------------------------------------------------------------------- #

def _svc(monkeypatch_sink: str = ""):
    """测试用的服务实例（带独立的临时记忆库）。"""
    from luxun_qq.service import BotService

    cfg = Config.from_dict({
        "llm": {"backend": "echo"},
        "bot": {"target_peer": "1987654321", "owner": "1234567890",
                "owner_sink": monkeypatch_sink},
        "memory": {"enabled": True, "db_path": _mem_db("svc"),
                   "extract_facts": False},
        "sender": {"adapter": "dry_run", "self_id": "1234567890"},
    })
    logs: list[str] = []
    return BotService(cfg, log=logs.append), logs


def _event(uid, text, mid):
    return {"post_type": "message", "message_type": "private", "user_id": uid,
            "message_id": mid,
            "message": [{"type": "text", "data": {"text": text}}]}


def test_service_generates_candidates_for_target_only() -> None:
    svc, _ = _svc()
    r = svc.handle_event(_event(1987654321, "今天又加班到这个点，你睡了吗", 1))
    assert r["action"] == "candidates_sent"
    assert "1987654321" in svc.pending
    # 其他对象不触发
    r2 = svc.handle_event(_event(1054549567, "你好", 2))
    assert r2["handled"] is False


def test_service_dedupes_same_message_id() -> None:
    svc, _ = _svc()
    svc.handle_event(_event(1987654321, "在吗", 7))
    again = svc.handle_event(_event(1987654321, "在吗", 7))
    assert again["handled"] is False
    assert "重复" in again["reason"]


def test_service_owner_selection_sends_and_clears() -> None:
    svc, _ = _svc()
    svc.handle_event(_event(1987654321, "在吗", 1))
    r = svc.handle_event(_event(1234567890, "1", 2))
    assert r["action"] == "sent" and r["ok"] is True
    assert not svc.pending, "发送后待选必须清空"


def test_service_stranger_cannot_trigger_send() -> None:
    svc, _ = _svc()
    svc.handle_event(_event(1987654321, "在吗", 1))
    r = svc.handle_event(_event(999999, "1", 2))
    assert r["handled"] is False
    assert svc.pending, "陌生人不该清掉待选项"


def test_service_out_of_range_index_keeps_pending() -> None:
    svc, _ = _svc()
    svc.handle_event(_event(1987654321, "在吗", 1))
    r = svc.handle_event(_event(1234567890, "9", 2))
    assert r["action"] == "invalid_index"
    assert svc.pending, "越界后仍应保持待选"


def test_service_drop_and_regen() -> None:
    svc, _ = _svc()
    svc.handle_event(_event(1987654321, "在吗", 1))
    assert svc.handle_event(_event(1234567890, "d", 2))["action"] == "dropped"
    assert not svc.pending

    svc.handle_event(_event(1987654321, "借我两千块钱急用", 3))
    assert svc.handle_event(_event(1234567890, "r", 4))["action"] == "candidates_sent"
    assert "1987654321" in svc.pending


def test_service_ignores_non_private_and_unknown_events() -> None:
    svc, _ = _svc()
    assert svc.handle_event({"post_type": "meta_event"})["handled"] is False
    group = _event(1987654321, "在吗", 1)
    group["message_type"] = "group"
    assert svc.handle_event(group)["handled"] is False
    assert not svc.pending


def test_service_owner_without_pending_does_nothing() -> None:
    svc, _ = _svc()
    r = svc.handle_event(_event(1234567890, "1", 1))
    assert r["handled"] is False


def test_service_notifies_owner_when_sink_configured() -> None:
    """owner_sink='self' 时，候选必须投递给 owner（而不只是写日志）。"""
    from luxun_qq.senders.base import SendResult

    class RecordingSender:
        name = "recording"

        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []

        def send_text(self, peer_id: str, text: str) -> SendResult:
            self.calls.append((peer_id, text))
            return SendResult.success("recording", "ok")

    svc, _ = _svc(monkeypatch_sink="self")
    rec = RecordingSender()
    svc.sender = rec
    svc.handle_event(_event(1987654321, "在吗", 1))

    assert rec.calls, "owner_sink=self 时应向 owner 投递候选"
    assert rec.calls[0][0] == "1234567890", "候选应发给 owner"
    assert "待你选择" in rec.calls[0][1]


def test_extract_text_handles_string_and_segments() -> None:
    from luxun_qq.service import _extract_text

    assert _extract_text("你好") == "你好"
    # 图片现在翻成可读描述（旧实现是 "[图片]" 占位）
    out = _extract_text([{"type": "text", "data": {"text": "a"}},
                         {"type": "image", "data": {}},
                         {"type": "text", "data": {"text": "b"}}])
    assert out.startswith("a") and out.endswith("b")
    assert "图" in out
    assert _extract_text(None) == ""


def test_webhook_server_accepts_event_and_health() -> None:
    """起一个真实 HTTP 服务，走一遍 webhook -> 生成候选。"""
    import threading
    import urllib.request

    from luxun_qq.service import make_server

    svc, _ = _svc()
    srv = make_server(svc.cfg, svc, host="127.0.0.1", port=0, access_token="")
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        base = f"http://127.0.0.1:{port}"
        health = json.loads(urllib.request.urlopen(base + "/health", timeout=5).read())
        assert health["status"] == "ok"

        # 用一个全新的 message_id：服务内部有去重表，复用旧 id 会被判为重复事件
        mid = 900000 + (port % 100000)
        body = json.dumps(_event(1987654321, "在吗", mid)).encode("utf-8")
        req = urllib.request.Request(base + "/", data=body,
                                     headers={"Content-Type": "application/json"})
        resp = json.loads(urllib.request.urlopen(req, timeout=10).read())
        assert resp["result"].get("action") == "candidates_sent", resp
    finally:
        srv.shutdown()
        srv.server_close()
        t.join(timeout=5)


# --------------------------------------------------------------------------- #
# 6c. WebSocket 客户端（手写 RFC 6455）
# --------------------------------------------------------------------------- #

def test_ws_client_handshake_and_echo() -> None:
    """起一个极小的 WebSocket 回显服务，验证握手/掩码/帧收发/echo 匹配。"""
    import base64 as _b64
    import hashlib as _hl
    import socket as _sock
    import struct as _st
    import threading as _th

    from luxun_qq.wsclient import WSClient
    from luxun_qq.senders.base import SendResult
    from luxun_qq.senders.ws import WsSender

    MAGIC = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
    srv = _sock.socket()
    srv.setsockopt(_sock.SOL_SOCKET, _sock.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    received: list[str] = []

    def serve() -> None:
        conn, _ = srv.accept()
        buf = b""
        while b"\r\n\r\n" not in buf:
            buf += conn.recv(4096)
        head = buf.split(b"\r\n\r\n")[0].decode("latin-1")
        key = [ln.split(":", 1)[1].strip() for ln in head.split("\r\n")
               if ln.lower().startswith("sec-websocket-key")][0]
        accept = _b64.b64encode(
            _hl.sha1((key + MAGIC).encode()).digest()).decode()
        conn.sendall((f"HTTP/1.1 101 Switching Protocols\r\n"
                      f"Upgrade: websocket\r\nConnection: Upgrade\r\n"
                      f"Sec-WebSocket-Accept: {accept}\r\n\r\n").encode())

        for _ in range(2):     # 收两帧：get_login_info、send_private_msg
            b1, b2 = conn.recv(2)
            ln = b2 & 0x7F
            if ln == 126:
                ln = _st.unpack(">H", conn.recv(2))[0]
            mask = conn.recv(4) if (b2 & 0x80) else b""
            payload = conn.recv(ln)
            if mask:
                payload = bytes(payload[i] ^ mask[i % 4] for i in range(ln))
            req = json.loads(payload.decode())
            received.append(req.get("action", ""))
            reply = json.dumps({
                "status": "ok", "retcode": 0,
                "data": {"user_id": 1234567890, "message_id": 42},
                "echo": req.get("echo"),
            }, ensure_ascii=False).encode()
            conn.sendall(bytes([0x81, len(reply)]) + reply)
        conn.close()

    _th.Thread(target=serve, daemon=True).start()

    client = WSClient(f"ws://127.0.0.1:{port}", timeout=10)
    client.connect()
    sender = WsSender(client, tmpdir(), self_id="1234567890", timeout=10)

    info = sender.get_login_info()
    assert info.get("user_id") == 1234567890, info

    res = sender.send_text("1987654321", "测试消息")
    assert isinstance(res, SendResult)
    assert res.ok, res.detail
    assert received == ["get_login_info", "send_private_msg"], received
    client.close()
    srv.close()


def test_ws_url_derivation() -> None:
    from luxun_qq.config import Config
    from luxun_qq.service_ws import _derive_ws_url

    cfg = Config.from_dict({"bot": {"ws_url": "ws://127.0.0.1:3001"}})
    assert _derive_ws_url(cfg) == "ws://127.0.0.1:3001"

    cfg2 = Config.from_dict({"bot": {"ws_url": ""},
                             "sender": {"base_url": "http://127.0.0.1:3001"}})
    assert _derive_ws_url(cfg2).startswith("ws://")


def test_ws_sender_reports_failure_on_error_response() -> None:
    """NapCat 返回 retcode != 0 时必须判为失败。"""
    from luxun_qq.senders.ws import WsSender

    class FakeClient:
        def send_json(self, obj):
            self.sent = obj

        def recv(self):
            return json.dumps({"status": "failed", "retcode": 1404,
                               "message": "不存在的用户",
                               "echo": self.sent["echo"]}, ensure_ascii=False)

    s = WsSender(FakeClient(), tmpdir(), self_id="1", timeout=5)
    res = s.send_text("99999", "hi")
    assert res.ok is False
    assert "失败" in res.detail or "不存在" in res.detail


# --------------------------------------------------------------------------- #
# 6d. 自动托管（输入 0）与拟人延迟
# --------------------------------------------------------------------------- #

def _recording_svc(**bot_overrides):
    """返回 (服务, 记录发送的 sender, 日志列表)，延迟默认关掉便于测试。"""
    from luxun_qq.service import BotService

    class RecordingSender:
        name = "recording"

        def __init__(self):
            self.calls: list[tuple[str, str]] = []

        def send_text(self, peer_id, text):
            from luxun_qq.senders.base import SendResult
            self.calls.append((str(peer_id), text))
            return SendResult.success("recording", "ok")

    bot = {"target_peer": "1987654321", "owner": "1234567890",
           "owner_sink": "self", "send_delay_min": 0, "send_delay_max": 0}
    bot.update(bot_overrides)
    cfg = Config.from_dict({
        "llm": {"backend": "echo"},
        "bot": bot,
        # 每个测试一个独立的临时库，避免互相抢锁 / 污染真实库
        "memory": {"enabled": True, "db_path": _mem_db("rec"),
                   "extract_facts": False},
        "sender": {"adapter": "dry_run", "self_id": "1234567890"},
    })
    logs: list[str] = []
    svc = BotService(cfg, log=logs.append)
    rec = RecordingSender()
    svc.sender = rec
    return svc, rec, logs


def _msg(uid, text, mid):
    return {"post_type": "message", "message_type": "private",
            "user_id": uid, "self_id": 1234567890, "message_id": mid,
            "message": [{"type": "text", "data": {"text": text}}]}


def test_auto_reply_zero_takes_over_and_sends() -> None:
    svc, rec, _ = _recording_svc()
    svc.handle_event(_msg(1987654321, "在干嘛呢", 101))
    before = len([c for c in rec.calls if c[0] == "1987654321"])

    r = svc.handle_event(_msg(1234567890, "0", 102))
    assert r.get("action") == "auto_sent", r
    assert r.get("auto_mode") == "on"
    assert "1987654321" in svc.auto_peers, "输入 0 后应进入托管"
    assert len([c for c in rec.calls if c[0] == "1987654321"]) == before + 1


def test_auto_reply_handles_subsequent_messages_without_asking() -> None:
    svc, rec, _ = _recording_svc()
    svc.handle_event(_msg(1987654321, "第一条", 201))
    svc.handle_event(_msg(1234567890, "0", 202))
    before = len([c for c in rec.calls if c[0] == "1987654321"])

    r = svc.handle_event(_msg(1987654321, "第二条", 203))
    assert r.get("action") == "auto_sent", r
    assert len([c for c in rec.calls if c[0] == "1987654321"]) == before + 1
    assert not svc.pending, "托管模式下不该产生待选"


def test_auto_reply_can_be_cancelled() -> None:
    svc, _, _ = _recording_svc()
    svc.handle_event(_msg(1987654321, "x", 301))
    svc.handle_event(_msg(1234567890, "0", 302))
    assert svc.auto_peers

    r = svc.handle_event(_msg(1234567890, "n", 303))
    assert r.get("action") == "auto_off_all"
    assert not svc.auto_peers

    r2 = svc.handle_event(_msg(1987654321, "y", 304))
    assert r2.get("action") == "candidates_sent", "取消托管后应恢复人工选择"


def test_auto_reply_toggle_command() -> None:
    svc, _, _ = _recording_svc()
    r1 = svc.handle_event(_msg(1234567890, "a", 401))     # 开启
    assert r1.get("action") == "auto_on"
    assert "1987654321" in svc.auto_peers
    r2 = svc.handle_event(_msg(1234567890, "a", 402))     # 再按一次关闭
    assert r2.get("action") == "auto_off"
    assert not svc.auto_peers


def test_send_delay_is_applied_within_range() -> None:
    """延迟应落在配置区间内（用 1~1.5 秒验证，避免测试太久）。"""
    import time as _t

    svc, _, _ = _recording_svc(send_delay_min=1, send_delay_max=1.5)
    svc.handle_event(_msg(1987654321, "延迟测试", 501))
    t0 = _t.time()
    svc.handle_event(_msg(1234567890, "1", 502))
    dt = _t.time() - t0
    assert 0.9 <= dt <= 2.5, f"延迟不在预期范围：{dt:.2f}s"


def test_conversation_memory_feeds_context() -> None:
    svc, _, _ = _recording_svc()
    svc.handle_event(_msg(1987654321, "我叫小明", 601))
    svc.handle_event(_msg(1234567890, "1", 602))
    svc.handle_event(_msg(1987654321, "我叫什么", 603))

    hist = svc.history_messages("1987654321")
    texts = [m.text for m in hist]
    assert "我叫小明" in texts
    assert "我叫什么" in texts
    # 我发出去的那条也应被记住
    assert any(m.direction.is_mine for m in hist)


def test_persona_switch_changes_candidate_labels() -> None:
    from luxun_qq.personas import PERSONAS, get_persona

    assert "emotion_master_male" in PERSONAS
    assert "luxun" in PERSONAS

    em = get_persona("emotion_master_male")
    assert em.candidate_labels() == ["情商线", "直球线", "稳一手"]
    assert "男方视角" in em.label

    lx = get_persona("luxun")
    assert lx.candidate_labels() == ["一锐", "一软", "一退"]

    # 未知 id 必须回退而不是抛异常
    assert get_persona("不存在的人格").id == "emotion_master_male"


def test_engine_uses_configured_persona() -> None:
    cfg = Config.from_dict({"llm": {"backend": "echo"},
                            "persona": "emotion_master_male"})
    cfg.retrieve.enabled = False
    eng = ReplyEngine(cfg, client=EchoClient(cfg.llm))
    assert eng.persona_obj.id == "emotion_master_male"
    assert eng.candidate_labels() == ["情商线", "直球线", "稳一手"]
    assert "男方视角" in eng.persona

    cfg2 = Config.from_dict({"llm": {"backend": "echo"}, "persona": "luxun"})
    cfg2.retrieve.enabled = False
    eng2 = ReplyEngine(cfg2, client=EchoClient(cfg2.llm))
    assert eng2.candidate_labels() == ["一锐", "一软", "一退"]


# --------------------------------------------------------------------------- #
# 6e. 多对象模式（target_peer 为 "*" 时任何人都是目标）
# --------------------------------------------------------------------------- #

_OWNER = "1234567890"
_PA = "1987654321"
_PB = "2222222222"
_PC = "3333333333"


def _multi_svc(target="*"):
    return _recording_svc(target_peer=target)


def _ev(uid, text, mid):
    return {"post_type": "message", "message_type": "private", "user_id": uid,
            "self_id": _OWNER, "message_id": mid,
            "message": [{"type": "text", "data": {"text": text}}]}


def test_wildcard_target_accepts_any_peer() -> None:
    svc, _, _ = _multi_svc()
    for i, uid in enumerate([_PA, _PB, _PC], 1):
        r = svc.handle_event(_ev(uid, f"消息{i}", i))
        assert r.get("action") == "candidates_sent", (uid, r)
    assert sorted(svc.pending) == sorted([_PA, _PB, _PC])


def test_wildcard_target_still_excludes_owner() -> None:
    """owner 的消息只当审批指令，绝不生成候选。"""
    svc, _, _ = _multi_svc()
    r = svc.handle_event(_ev(_OWNER, "我自己说的话", 1))
    assert r.get("handled") is False
    assert not svc.pending


def test_explicit_peer_prefix_routes_to_that_peer() -> None:
    """`<QQ号> <序号>` 精确指定对象，避免多人同时待选时选错。"""
    svc, rec, _ = _multi_svc()
    svc.handle_event(_ev(_PA, "a", 1))
    svc.handle_event(_ev(_PB, "b", 2))

    r = svc.handle_event(_ev(_OWNER, f"{_PA} 1", 3))
    assert r.get("action") == "sent"
    assert r.get("peer_id") == _PA
    assert len([c for c in rec.calls if c[0] == _PA]) == 1
    assert len([c for c in rec.calls if c[0] == _PB]) == 0, "B 不该被发送"
    assert _PB in svc.pending, "B 的待选应保留"


def test_bare_index_targets_most_recent_pending() -> None:
    svc, rec, _ = _multi_svc()
    svc.handle_event(_ev(_PA, "a", 1))
    svc.handle_event(_ev(_PB, "b", 2))
    r = svc.handle_event(_ev(_OWNER, "1", 3))
    assert r.get("peer_id") == _PB, "不带号应作用于最近收到的那条"
    assert _PA in svc.pending


def test_command_for_unknown_peer_does_not_send() -> None:
    svc, rec, _ = _multi_svc()
    svc.handle_event(_ev(_PA, "a", 1))
    r = svc.handle_event(_ev(_OWNER, "9999999999 1", 2))
    assert r.get("action") == "no_pending_for_peer"
    assert len([c for c in rec.calls if c[0] == _PA]) == 0


def test_auto_reply_applies_per_peer_only() -> None:
    """对一个对象开启托管，不应影响其他对象。"""
    svc, rec, _ = _multi_svc()
    svc.handle_event(_ev(_PA, "1", 1))
    svc.handle_event(_ev(_PB, "2", 2))
    svc.handle_event(_ev(_OWNER, "0", 3))          # 作用于最近的 PB
    assert _PB in svc.auto_peers
    assert _PA not in svc.auto_peers

    r = svc.handle_event(_ev(_PB, "再来", 4))
    assert r.get("action") == "auto_sent"
    r2 = svc.handle_event(_ev(_PA, "我也来", 5))
    assert r2.get("action") == "candidates_sent", "A 未托管，仍应询问我"


def test_auto_reply_all_peers_via_wildcard() -> None:
    """无人待选时按 a，在通配模式下开启对所有人的托管。"""
    svc, rec, logs = _multi_svc()
    r = svc.handle_event(_ev(_OWNER, "a", 1))
    assert r.get("action") == "auto_on"
    assert "*" in svc.auto_peers

    for i, uid in enumerate([_PA, _PB, _PC], 2):
        rr = svc.handle_event(_ev(uid, f"喂{i}", i))
        assert rr.get("action") == "auto_sent", (uid, rr)

    r2 = svc.handle_event(_ev(_OWNER, "n", 9))
    assert r2.get("action") == "auto_off_all"
    assert not svc.auto_peers
    assert svc.handle_event(_ev(_PA, "取消后", 10)).get("action") == "candidates_sent"


def test_single_target_mode_still_works() -> None:
    """显式指定单个对象时，其他人不生成候选（向后兼容）。"""
    svc, _, _ = _multi_svc(target=_PA)
    r = svc.handle_event(_ev(_PB, "我不是目标", 1))
    assert r.get("handled") is False
    r2 = svc.handle_event(_ev(_PA, "我是目标", 2))
    assert r2.get("action") == "candidates_sent"
    assert list(svc.pending) == [_PA]


def test_target_all_property_recognises_forms() -> None:
    from luxun_qq.service import BotService

    for form in ("", "*", "all", "ALL", " * "):
        cfg = Config.from_dict({"llm": {"backend": "echo"},
                                "bot": {"target_peer": form, "owner": _OWNER},
                                "sender": {"adapter": "dry_run",
                                           "self_id": _OWNER}})
        svc = BotService(cfg, log=lambda *_: None)
        assert svc.target_all is True, form

    cfg2 = Config.from_dict({"llm": {"backend": "echo"},
                             "bot": {"target_peer": _PA, "owner": _OWNER},
                             "sender": {"adapter": "dry_run", "self_id": _OWNER}})
    assert BotService(cfg2, log=lambda *_: None).target_all is False


def test_parse_command_splits_peer_prefix() -> None:
    from luxun_qq.service import BotService

    p = BotService._parse_command
    assert p("1") == ("", "1")
    assert p("  2 ") == ("", "2")
    assert p("1987654321 3") == ("1987654321", "3")
    assert p("a") == ("", "a")
    # 短数字不算 QQ 号，避免把 "10" 误当对象
    assert p("10 1") == ("", "10 1")


# --------------------------------------------------------------------------- #
# 6f. 白名单（可热编辑的回复范围）
# --------------------------------------------------------------------------- #

def _wl_path(name: str = "whitelist.txt"):
    """测试用的白名单文件路径（tmpdir 返回 str，这里转成 Path）。"""
    from pathlib import Path

    return Path(tmpdir()) / name


def test_split_qq_numbers_parses_all_separators() -> None:
    from luxun_qq.service import split_qq_numbers

    assert split_qq_numbers("1987654321") == {"1987654321"}
    assert split_qq_numbers("1987654321, 2222222222") == {"1987654321", "2222222222"}
    assert split_qq_numbers("1987654321 2222222222") == {"1987654321", "2222222222"}
    assert split_qq_numbers("1987654321、2222222222") == {"1987654321", "2222222222"}
    assert split_qq_numbers("1987654321;2222222222") == {"1987654321", "2222222222"}
    # 通配与说明文字应被忽略，短数字也不算号
    assert split_qq_numbers("*") == set()
    assert split_qq_numbers("all") == set()
    assert split_qq_numbers("") == set()
    assert split_qq_numbers("10 1") == set()
    # 混在注释文字里也能提取
    assert split_qq_numbers("目标 1987654321 优先") == {"1987654321"}


def test_whitelist_file_restricts_to_listed_numbers() -> None:
    from luxun_qq.service import BotService

    wl = _wl_path()
    wl.write_text("1987654321\n", encoding="utf-8")
    cfg = Config.from_dict({
        "llm": {"backend": "echo"},
        "bot": {"target_mode": "whitelist", "whitelist_file": str(wl),
                "owner": _OWNER, "owner_sink": "", "send_delay_min": 0,
                "send_delay_max": 0},
        "sender": {"adapter": "dry_run", "self_id": _OWNER},
    })
    svc = BotService(cfg, log=lambda *_: None)

    assert svc.target_mode == "whitelist"
    assert svc.load_whitelist() == {"1987654321"}
    assert svc._is_target("1987654321") is True
    assert svc._is_target("2222222222") is False
    assert svc._is_target(_OWNER) is False, "owner 永远排除"


def test_whitelist_supports_comments_and_multiple_per_line() -> None:
    from luxun_qq.service import BotService

    wl = _wl_path()
    wl.write_text(
        "# 这是注释\n"
        "\n"
        "1987654321          # 行内注释\n"
        "2222222222, 3333333333\n",
        encoding="utf-8")
    cfg = Config.from_dict({
        "llm": {"backend": "echo"},
        "bot": {"target_mode": "whitelist", "whitelist_file": str(wl),
                "owner": _OWNER, "owner_sink": ""},
        "sender": {"adapter": "dry_run", "self_id": _OWNER},
    })
    svc = BotService(cfg, log=lambda *_: None)
    assert svc.load_whitelist() == {"1987654321", "2222222222", "3333333333"}


def test_whitelist_is_hot_reloaded() -> None:
    """改白名单文件应立即生效，不需要重建服务。"""
    import time as _t

    from luxun_qq.service import BotService

    wl = _wl_path()
    wl.write_text("1987654321\n", encoding="utf-8")
    cfg = Config.from_dict({
        "llm": {"backend": "echo"},
        "bot": {"target_mode": "whitelist", "whitelist_file": str(wl),
                "owner": _OWNER, "owner_sink": ""},
        "sender": {"adapter": "dry_run", "self_id": _OWNER},
    })
    svc = BotService(cfg, log=lambda *_: None)
    assert svc._is_target("2222222222") is False

    _t.sleep(1.1)                      # 让 mtime 真的变化
    wl.write_text("1987654321\n2222222222\n", encoding="utf-8")
    assert svc._is_target("2222222222") is True, "白名单改动应立即生效"

    _t.sleep(1.1)
    wl.write_text("# 全部注释掉\n", encoding="utf-8")
    assert svc.load_whitelist() == set()
    assert svc._is_target("1987654321") is False, "名单为空时谁也不回"


def test_empty_whitelist_replies_to_nobody_and_warns_once() -> None:
    from luxun_qq.service import BotService

    wl = _wl_path()
    wl.write_text("# 空的\n", encoding="utf-8")
    logs: list[str] = []
    cfg = Config.from_dict({
        "llm": {"backend": "echo"},
        "bot": {"target_mode": "whitelist", "whitelist_file": str(wl),
                "owner": _OWNER, "owner_sink": ""},
        "sender": {"adapter": "dry_run", "self_id": _OWNER},
    })
    svc = BotService(cfg, log=logs.append)

    assert svc._is_target("1987654321") is False
    assert svc._is_target("2222222222") is False
    warn = [x for x in logs if "名单为空" in x]
    assert len(warn) == 1, "空名单提示只应打印一次，避免刷屏"

    # 事件层面的表现：被跳过且原因说明是范围问题
    r = svc.handle_event(_ev("1987654321", "你好", 1))
    assert r.get("handled") is False
    assert "不在回复范围" in r.get("reason", "")


def test_missing_whitelist_file_does_not_crash() -> None:
    from luxun_qq.service import BotService

    cfg = Config.from_dict({
        "llm": {"backend": "echo"},
        "bot": {"target_mode": "whitelist",
                "whitelist_file": str(_wl_path("根本不存在.txt")),
                "owner": _OWNER, "owner_sink": ""},
        "sender": {"adapter": "dry_run", "self_id": _OWNER},
    })
    svc = BotService(cfg, log=lambda *_: None)
    assert svc.load_whitelist() == set()
    assert svc._is_target("1987654321") is False


def test_target_peer_list_combines_with_whitelist_file() -> None:
    """target_peer 里写的号与白名单文件里的号取并集。"""
    from luxun_qq.service import BotService

    wl = _wl_path()
    wl.write_text("2222222222\n", encoding="utf-8")
    cfg = Config.from_dict({
        "llm": {"backend": "echo"},
        "bot": {"target_mode": "whitelist", "whitelist_file": str(wl),
                "target_peer": "1987654321", "owner": _OWNER, "owner_sink": ""},
        "sender": {"adapter": "dry_run", "self_id": _OWNER},
    })
    svc = BotService(cfg, log=lambda *_: None)
    assert svc.load_whitelist() == {"1987654321", "2222222222"}
    assert svc._is_target("1987654321") and svc._is_target("2222222222")


def test_mode_inferred_from_target_peer_when_not_set() -> None:
    """未显式配 target_mode 时的向后兼容推断。"""
    from luxun_qq.service import BotService

    def mode_of(target_peer, target_mode=""):
        cfg = Config.from_dict({
            "llm": {"backend": "echo"},
            "bot": {"target_peer": target_peer, "target_mode": target_mode,
                    "owner": _OWNER, "whitelist_file": "",
                    "owner_sink": ""},
            "sender": {"adapter": "dry_run", "self_id": _OWNER}})
        return BotService(cfg, log=lambda *_: None).target_mode

    assert mode_of("*") == "all"
    assert mode_of("") == "all"
    assert mode_of("1987654321") == "whitelist"
    # 显式配置优先
    assert mode_of("1987654321", "all") == "all"
    assert mode_of("*", "whitelist") == "whitelist"


# --------------------------------------------------------------------------- #
# 6g. 长期记忆
# --------------------------------------------------------------------------- #

def _mem_store(name: str = "mem.db"):
    from luxun_qq.memory import MemoryStore

    p = _wl_path(name)
    p.unlink(missing_ok=True)
    return MemoryStore(p), p


def test_memory_chinese_bigram_tokenization() -> None:
    from luxun_qq.memory import split_tokens

    toks = split_tokens("今天上班好累")
    assert "上班" in toks and "好累" in toks, toks
    # 单字不应出现（否则检索退化成噪声）
    assert "好" not in toks
    # 停用词被过滤
    assert "什么" not in split_tokens("什么")
    # 英文与数字
    assert "hello" in split_tokens("hello 测试")


def test_memory_stores_and_recalls_old_messages() -> None:
    """核心能力：很久以前说的话，之后还能按关键词召回。"""
    import time as _t

    m, path = _mem_store("mem_recall.db")
    try:
        now = _t.time()
        m.add_message("1987654321", mine=False, text="我下周三要去面试，有点紧张",
                      msg_id="1", ts=now - 100000)
        for i in range(2, 40):
            m.add_message("1987654321", mine=False, text=f"闲聊第{i}句",
                          msg_id=str(i), ts=now - (100 - i) * 10)

        hits = m.recall("1987654321", "面试准备得怎么样了", limit=5)
        assert hits, "应该能召回面试那条"
        assert any("面试" in h.text for h in hits)
        # 只该出现一次（去重）
        assert len([h for h in hits if "面试" in h.text]) == 1
    finally:
        m.close()
        path.unlink(missing_ok=True)


def test_memory_dedupes_by_msg_id() -> None:
    m, path = _mem_store("mem_dedup.db")
    try:
        assert m.add_message("1", mine=False, text="同一句", msg_id="42") is True
        assert m.add_message("1", mine=False, text="同一句", msg_id="42") is False
        assert m.stats("1")["messages"] == 1
    finally:
        m.close()
        path.unlink(missing_ok=True)


def test_memory_facts_roundtrip() -> None:
    m, path = _mem_store("mem_facts.db")
    try:
        assert m.add_fact("1", "事件", "下周三要面试") is True
        # 重复内容不再新增，但时间戳刷新
        assert m.add_fact("1", "事件", "下周三要面试") is False
        facts = m.facts("1")
        assert len(facts) == 1 and facts[0].content == "下周三要面试"
        assert "面试" in facts[0].line()
        # 关键词召回事实
        assert m.recall_facts("1", "面试怎么样")
    finally:
        m.close()
        path.unlink(missing_ok=True)


def test_memory_recent_returns_chronological() -> None:
    import time as _t

    m, path = _mem_store("mem_recent.db")
    try:
        now = _t.time()
        for i in range(5):
            m.add_message("1", mine=bool(i % 2), text=f"第{i}句",
                          msg_id=str(i), ts=now + i)
        rows = m.recent("1", 3)
        assert [r.text for r in rows] == ["第2句", "第3句", "第4句"]
    finally:
        m.close()
        path.unlink(missing_ok=True)


def test_memory_forget_removes_everything() -> None:
    m, path = _mem_store("mem_forget.db")
    try:
        m.add_message("1", mine=False, text="记得我", msg_id="1")
        m.add_fact("1", "身份", "我是小明")
        info = m.forget("1")
        assert info == {"messages": 1, "facts": 1}
        assert m.stats("1")["messages"] == 0
        assert m.facts("1") == []
        assert m.recall("1", "记得我") == []
    finally:
        m.close()
        path.unlink(missing_ok=True)


def test_memory_face_names_learn_and_persist() -> None:
    m, path = _mem_store("mem_face.db")
    try:
        m.learn_face("9999", "新表情")
        m.learn_face("9999", "新表情")      # 重复学习只累加 seen
        assert m.face_names()["9999"] == "新表情"
    finally:
        m.close()
        # 重开一个实例，验证真的落盘了
        from luxun_qq.memory import MemoryStore

        m2 = MemoryStore(path)
        assert m2.face_names()["9999"] == "新表情"
        m2.close()
        path.unlink(missing_ok=True)


def test_service_writes_conversation_to_memory() -> None:
    """服务层：收到消息应落库，并能在之后召回。"""
    from luxun_qq.service import BotService

    cfg = Config.from_dict({
        "llm": {"backend": "echo"},
        "bot": {"target_peer": "1987654321", "owner": _OWNER, "owner_sink": "",
                "send_delay_min": 0, "send_delay_max": 0},
        # 路径由 LUXUN_MEMORY_DB 统一重定向到临时目录（见文件顶部说明）
        "memory": {"enabled": True, "extract_facts": False},
        "sender": {"adapter": "dry_run", "self_id": _OWNER},
    })
    logs: list[str] = []
    svc = BotService(cfg, log=logs.append)
    try:
        assert svc.memory is not None, f"记忆库未能打开，日志：{logs}"
        svc.handle_event(_ev("1987654321", "我妈生日是下个月八号", 1))
        assert svc.memory.stats("1987654321")["messages"] == 1

        ctx = svc.build_memory_context("1987654321", "生日礼物买什么好")
        assert "old_messages" in ctx and "facts" in ctx

        # 短期上下文应从长期库读取
        hist = svc.history_messages("1987654321")
        assert any("生日" in m.text for m in hist)
    finally:
        if svc.memory:
            svc.memory.close()


def test_service_memory_block_reaches_prompt() -> None:
    """记忆要真的进到提示词里，否则等于没做。"""
    from luxun_qq.engine import ReplyEngine
    from luxun_qq.memory import MemoryFact, MemoryMessage

    cfg = Config.from_dict({"llm": {"backend": "echo"},
                            "persona": "emotion_master_male"})
    cfg.retrieve.enabled = False
    eng = ReplyEngine(cfg, client=EchoClient(cfg.llm))

    block = eng._memory_block({
        "facts": [MemoryFact(kind="事件", content="下周三要面试")],
        "old_messages": [MemoryMessage(ts=0, mine=False, text="我有点紧张")],
    })
    assert "下周三要面试" in block
    assert "我有点紧张" in block
    # 必须带"别生硬复述"的约束，否则模型会写查户口式的句子
    assert "不要生硬复述" in block
    # 无记忆时不应产生任何片段
    assert eng._memory_block(None) == ""
    assert eng._memory_block({"facts": [], "old_messages": []}) == ""


def test_engine_pads_fewer_than_three_candidates() -> None:
    """模型只给出 1-2 条候选时必须补齐，否则"三选一"的核心体验就废了。"""
    from luxun_qq.engine import (Candidate, ReplyEngine, ReplyPlan, Step1Read,
                                 Step2Emotion, Step3Strategy)

    plan = ReplyPlan(
        step1=Step1Read(literal="x", intent="y"),
        step2=Step2Emotion(emotion="e", intensity=0.5, need="n", risk="r"),
        step3=Step3Strategy(strategy="s", tone="t",
                            candidates=[Candidate(text="唯一一条", why="w")]))
    out = ReplyEngine._pad_candidates(plan)
    assert len(out.step3.candidates) == 3
    assert out.step3.candidates[0].text == "唯一一条"
    assert "补齐" in out.note

    # 已经够 3 条时不动
    plan2 = ReplyPlan(
        step1=Step1Read(literal="x", intent="y"),
        step2=Step2Emotion(emotion="e", intensity=0.5, need="n", risk="r"),
        step3=Step3Strategy(strategy="s", tone="t", candidates=[
            Candidate(text="a"), Candidate(text="b"), Candidate(text="c")]))
    assert ReplyEngine._pad_candidates(plan2) is plan2
    assert plan2.note == ""


def test_service_narrows_prompt_history() -> None:
    """喂给提示词的历史必须比入库窗口小很多（否则模型被最近话题锚住）。"""
    from luxun_qq.service import BotService

    cfg = Config.load()
    assert cfg.memory.prompt_history_limit < cfg.memory.recent_limit
    assert cfg.memory.prompt_history_limit <= 6

    db = _wl_path("hist.db")
    db.unlink(missing_ok=True)
    cfg2 = Config.from_dict({
        "llm": {"backend": "echo"},
        "bot": {"target_peer": "1987654321", "owner": _OWNER, "owner_sink": "",
                "send_delay_min": 0, "send_delay_max": 0},
        "memory": {"enabled": True, "db_path": str(db),
                   "prompt_history_limit": 2, "extract_facts": False},
        "sender": {"adapter": "dry_run", "self_id": _OWNER},
    })
    svc = BotService(cfg2, log=lambda *_: None)
    try:
        for i in range(6):
            svc.remember("1987654321", mine=False, text=f"第{i}句", msg_id=str(i))
        # 库里有 6 条，但喂给提示词只给 2 条
        assert len(svc.history_messages("1987654321")) == 6
        assert len(svc.history_messages("1987654321", limit=2)) == 2
    finally:
        if svc.memory:
            svc.memory.close()
        db.unlink(missing_ok=True)


def test_memory_recovers_from_corrupt_database() -> None:
    """库损坏时必须自愈，不能让整个服务起不来。"""
    from luxun_qq.memory import MemoryStore

    p = _wl_path("corrupt.db")
    p.unlink(missing_ok=True)
    p.write_bytes(b"SQLite format 3\x00" + b"\x00" * 200 + b"garbage" * 100)

    m = MemoryStore(p)
    try:
        assert m._recovered is True, "损坏的库应被识别并重建"
        assert m.add_message("1", mine=False, text="自愈后可用", msg_id="1") is True
        assert m.stats()["messages"] == 1
        assert m.stats()["recovered"] is True
    finally:
        m.close()
        for f in _wl_path("").glob("corrupt.db*"):
            f.unlink(missing_ok=True)


def test_service_survives_corrupt_memory_db() -> None:
    """记忆库坏了，服务仍应能正常回复（只是没记忆）。"""
    from luxun_qq.service import BotService

    p = _wl_path("corrupt2.db")
    p.unlink(missing_ok=True)
    p.write_bytes(b"not a database at all" * 20)

    cfg = Config.from_dict({
        "llm": {"backend": "echo"},
        "bot": {"target_peer": "1987654321", "owner": _OWNER,
                "owner_sink": "", "send_delay_min": 0, "send_delay_max": 0},
        "memory": {"enabled": True, "db_path": str(p), "extract_facts": False},
        "sender": {"adapter": "dry_run", "self_id": _OWNER},
    })
    svc = BotService(cfg, log=lambda *_: None)
    try:
        # 坏库会被识别并重建，所以 memory 不该是 None
        assert svc.memory is not None
        r = svc.handle_event(_ev("1987654321", "你好", 1))
        assert r.get("action") == "candidates_sent", r
    finally:
        if svc.memory:
            svc.memory.close()
        for f in _wl_path("").glob("corrupt2.db*"):
            try:
                f.unlink(missing_ok=True)
            except OSError:
                pass


# --------------------------------------------------------------------------- #
# 6h. QQ 表情解析
# --------------------------------------------------------------------------- #

def test_face_names_table_is_loaded() -> None:
    from luxun_qq.faces_data import FACE_NAMES

    assert len(FACE_NAMES) > 300, "内置表情表条目过少"
    assert FACE_NAMES["14"] == "微笑"
    assert FACE_NAMES["182"] == "笑哭"
    assert FACE_NAMES["5"] == "流泪"


def test_face_segment_becomes_readable_text() -> None:
    from luxun_qq.faces import segments_to_text

    # 只发一个表情，以前会被当成空消息丢掉
    r = segments_to_text([{"type": "face", "data": {"id": "182"}}])
    assert r.text == "（笑哭）"
    assert r.has_content is True
    assert r.kinds == ["face"]
    assert r.face_ids == ["182"]

    # 表情 + 文字
    r2 = segments_to_text([{"type": "text", "data": {"text": "哈哈哈"}},
                           {"type": "face", "data": {"id": "14"}}])
    assert "哈哈哈" in r2.text and "微笑" in r2.text


def test_unknown_face_falls_back_and_learns() -> None:
    from luxun_qq.faces import face_name, segments_to_text

    # 表里没有、事件也没给名字：保留 id 便于后续补齐
    assert face_name("8888") == "表情8888"

    # 事件给了 summary -> 用 summary，并且值得记进缓存
    learned: dict[str, str] = {}
    r = segments_to_text([{"type": "face",
                           "data": {"id": "9999", "summary": "[新表情]"}}],
                         learn=learned)
    assert r.text == "（新表情）"
    assert learned == {"9999": "新表情"}

    # 没有名字时不该把兜底名当知识存下来
    learned2: dict[str, str] = {}
    segments_to_text([{"type": "face", "data": {"id": "7777"}}], learn=learned2)
    assert learned2 == {}


def test_non_text_segments_are_not_dropped() -> None:
    """图片/语音/表情包/未知类型都不能被静默丢弃。"""
    from luxun_qq.faces import segments_to_text

    cases = [
        ([{"type": "mface", "data": {"summary": "[熊猫头]"}}], "表情包"),
        ([{"type": "image", "data": {"summary": "你礼貌吗"}}], "你礼貌吗"),
        ([{"type": "image", "data": {}}], "图"),
        ([{"type": "record", "data": {}}], "语音"),
        ([{"type": "unknown_thing", "data": {}}], "unknown_thing"),
    ]
    for msg, expect in cases:
        r = segments_to_text(msg)
        assert r.text, f"{msg} 被解析成空"
        assert expect in r.text, (msg, r.text)
        assert r.has_content is True, msg


def test_pure_reply_segment_is_not_content() -> None:
    from luxun_qq.faces import segments_to_text

    r = segments_to_text([{"type": "reply", "data": {"id": "1"}}])
    assert r.has_content is False, "只有引用标记时不算有内容"


def test_empty_message_array_has_no_content() -> None:
    from luxun_qq.faces import segments_to_text

    r = segments_to_text([])
    assert r.has_content is False and r.text == ""


# --------------------------------------------------------------------------- #
# 7. runner
# --------------------------------------------------------------------------- #

def _collect() -> list:
    return [(n, f) for n, f in sorted(globals().items())
            if n.startswith("test_") and callable(f)]


def main() -> int:
    tests = _collect()
    print(f"共 {len(tests)} 个测试\n")
    failed: list[tuple[str, str]] = []
    for i, (name, fn) in enumerate(tests, 1):
        try:
            fn()
            print(f"  [{i:>2}/{len(tests)}] PASS  {name}")
        except Exception:
            print(f"  [{i:>2}/{len(tests)}] FAIL  {name}")
            failed.append((name, traceback.format_exc()))

    for d in _CLEANUPS:
        shutil.rmtree(d, ignore_errors=True)
    # 清掉临时根目录（若空）
    run_root = os.path.join(ROOT, "tests", "_tmp_run")
    if os.path.isdir(run_root) and not os.listdir(run_root):
        os.rmdir(run_root)

    print()
    if failed:
        for name, tb in failed:
            print(f"===== {name} =====")
            print(tb)
        print(f"✗ {len(failed)}/{len(tests)} 个测试失败")
        return 1
    print(f"✓ 全部 {len(tests)} 个测试通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
