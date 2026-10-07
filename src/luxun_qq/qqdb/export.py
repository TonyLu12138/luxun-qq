# -*- coding: utf-8 -*-
"""从 nt_msg.db 导出私聊（C2C）消息记录。

nt_msg.db 的 c2c_msg_table 关键列（数字列名，腾讯 Protobuf 字段号）：
  40001 消息 ID          40050 时间戳(Unix 秒)     40013 方向(1=发出,0=收到)
  40020 发送者 UID       40033 发送者 QQ          40021 会话对象 UID
  40030 会话对象 QQ      40011 外层消息类型       40800 消息体(Protobuf)
  45101 文本（部分版本存在）

40800 是 Protobuf，内部再套 45101 文本段。本模块自带一个最小 Protobuf 解析器，
只提取文本，避免引入 protobuf 编译依赖；同时把原始 bytes 存进 raw 供后续深挖。
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional

from ..schema import Direction, Message, MsgKind

# ---------------------------------------------------------------- 最小 Protobuf

def _read_varint(buf: bytes, i: int) -> tuple[int, int]:
    result = 0
    shift = 0
    while True:
        if i >= len(buf):
            raise ValueError("varint 越界")
        b = buf[i]
        i += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            return result, i
        shift += 7
        if shift > 63:
            raise ValueError("varint 过长")


def parse_protobuf(buf: bytes) -> list[tuple[int, int, Any]]:
    """极简 protobuf 解析：返回 [(field_no, wire_type, value)]。

    wire_type: 0=varint, 1=fixed64, 2=length-delimited, 5=fixed32
    解析失败时返回已解析出的部分，不抛异常（脏数据很常见）。
    """
    out: list[tuple[int, int, Any]] = []
    i = 0
    n = len(buf)
    try:
        while i < n:
            key, i = _read_varint(buf, i)
            field, wt = key >> 3, key & 0x07
            if wt == 0:
                val, i = _read_varint(buf, i)
            elif wt == 1:
                val = buf[i:i + 8]
                i += 8
            elif wt == 2:
                ln, i = _read_varint(buf, i)
                val = buf[i:i + ln]
                i += ln
            elif wt == 5:
                val = buf[i:i + 4]
                i += 4
            else:  # 不支持的 wire type，停止
                break
            out.append((field, wt, val))
    except Exception:
        pass
    return out


TEXT_FIELDS = (45101,)          # 文本字段
_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def extract_text(blob: Optional[bytes], max_depth: int = 6) -> tuple[str, list[str]]:
    """从 40800 消息体里递归抽取文本与段类型。

    返回 (合并文本, 段类型列表)。段类型帮助判断图片/文件/表情等。
    """
    if not blob:
        return "", []

    kinds: list[str] = []
    texts: list[str] = []

    def walk(b: bytes, depth: int) -> None:
        if depth > max_depth or not b:
            return
        for field, wt, val in parse_protobuf(b):
            if wt != 2 or not isinstance(val, (bytes, bytearray)):
                continue
            if field in TEXT_FIELDS:
                s = _decode_text(bytes(val))
                if s:
                    texts.append(s)
                    kinds.append("text")
                continue
            # 段类型字段（45002 等）的值是 varint 之外的编码，这里靠递归兜底
            if len(val) > 1:
                walk(bytes(val), depth + 1)

    walk(blob, 0)
    text = "".join(texts)
    return text, kinds


def _decode_text(b: bytes) -> str:
    if not b:
        return ""
    # 优先 UTF-8；失败退回 GBK（老记录）
    for enc in ("utf-8", "gb18030"):
        try:
            s = b.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        return ""
    s = _CTRL_RE.sub("", s)
    return s.strip()


# ---------------------------------------------------------------- 表结构发现

@dataclass
class TableInfo:
    name: str
    columns: set[str]


def list_tables(con: sqlite3.Connection) -> list[TableInfo]:
    cur = con.cursor()
    infos = []
    for (name,) in cur.execute(
            "select name from sqlite_master where type='table' order by name"):
        cols = {r[1] for r in cur.execute(f'pragma table_info("{name}")')}
        infos.append(TableInfo(name, cols))
    return infos


def pick_c2c_table(tables: Iterable[TableInfo]) -> Optional[TableInfo]:
    """挑出 C2C 私聊消息表。"""
    prefer = ("c2c_msg_table", "c2c_msg", "buddy_msg_table")
    by_name = {t.name.lower(): t for t in tables}
    for p in prefer:
        if p in by_name:
            return by_name[p]
    # 回退：找同时含 40001 与 40800 的表
    for t in tables:
        if {"40001", "40800"} <= t.columns:
            return t
    return None


# ---------------------------------------------------------------- 导出

def _to_int(v: Any) -> Optional[int]:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def export_c2c(db_path: str | Path, *, me: str, peer: Optional[str] = None,
               limit: Optional[int] = None,
               batch: int = 5000) -> Iterator[Message]:
    """从明文 nt_msg.db 里流式导出 C2C 消息。

    me   : 我的 QQ 号，用于判定方向
    peer : 只导出与该对象的会话；None = 全部
    """
    con = sqlite3.connect(f"file:{Path(db_path).as_posix()}?mode=ro", uri=True)
    con.text_factory = bytes
    try:
        tables = list_tables(con)
        table = pick_c2c_table(tables)
        if table is None:
            raise RuntimeError(
                "未找到 C2C 消息表。实际表：" + ", ".join(t.name for t in tables))
        cols = table.columns

        def col(name: str) -> Optional[str]:
            return f'"{name}"' if name in cols else None

        sel = [c for c in ("40001", "40050", "40013", "40020", "40033",
                           "40021", "40030", "40011", "40800", "45101")
               if col(c)]
        if not sel:
            raise RuntimeError(f"表 {table.name} 缺少预期列，实际列：{sorted(cols)}")

        sql = f'select {", ".join(col(c) for c in sel)} from "{table.name}"'
        params: list[Any] = []
        if peer and col("40030"):
            sql += ' where "40030" = ?'
            params.append(int(peer))
        sql += ' order by "40050" asc'
        if limit:
            sql += f" limit {int(limit)}"

        cur = con.cursor()
        cur.execute(sql, params)
        idx = {c: i for i, c in enumerate(sel)}
        me_int = _to_int(me)

        while True:
            rows = cur.fetchmany(batch)
            if not rows:
                break
            for row in rows:
                msg = _row_to_message(row, idx, me_int, table.name)
                if msg is not None:
                    yield msg
    finally:
        con.close()


def _row_to_message(row: tuple, idx: dict[str, int], me: Optional[int],
                    source: str) -> Optional[Message]:
    def get(c: str) -> Any:
        i = idx.get(c)
        return row[i] if i is not None else None

    mid = get("40001")
    ts = _to_int(get("40050")) or _to_int(get("49155")) or 0
    sender_qq = _to_int(get("40033"))
    peer_qq = _to_int(get("40030"))
    direction_raw = _to_int(get("40013"))

    if mid is None:
        return None

    blob = get("40800")
    if isinstance(blob, str):
        blob = blob.encode("latin-1", "ignore")

    text, kinds = extract_text(blob if isinstance(blob, (bytes, bytearray)) else None)
    if not text:
        legacy = get("45101")
        if isinstance(legacy, (bytes, bytearray)):
            text = _decode_text(bytes(legacy))

    # 方向判定：优先用 40013；缺失时用发送者比对
    if direction_raw in (0, 1):
        direction = Direction.OUT if direction_raw == 1 else Direction.IN
    elif me is not None and sender_qq is not None:
        direction = Direction.OUT if sender_qq == me else Direction.IN
    else:
        direction = Direction.IN

    kind = _classify(text, kinds)
    # 会话对象：私聊里 peer 就是收发双方中的另一方
    if peer_qq is not None:
        peer_id = str(peer_qq)
    elif me is not None and sender_qq is not None:
        peer_id = str(peer_qq if direction is Direction.IN else sender_qq)
    else:
        peer_id = str(peer_qq or sender_qq or "")

    return Message(
        msg_id=str(mid),
        ts=int(ts),
        direction=direction,
        peer_id=peer_id,
        text=text,
        kind=kind,
        sender_id=str(sender_qq or ""),
        raw={"msg_type": _to_int(get("40011")), "kinds": kinds[:8]},
        source=source,
    )


def _classify(text: str, kinds: list[str]) -> MsgKind:
    if text:
        return MsgKind.TEXT
    if not kinds:
        return MsgKind.UNKNOWN
    return MsgKind.UNKNOWN


def count_c2c(db_path: str | Path) -> int:
    con = sqlite3.connect(f"file:{Path(db_path).as_posix()}?mode=ro", uri=True)
    try:
        table = pick_c2c_table(list_tables(con))
        if table is None:
            return 0
        return con.execute(f'select count(*) from "{table.name}"').fetchone()[0]
    finally:
        con.close()


def list_peers(db_path: str | Path, me: str, limit: int = 50) -> list[tuple[str, int]]:
    """列出聊天最多的会话对象 [(peer_qq, 消息数)]。"""
    con = sqlite3.connect(f"file:{Path(db_path).as_posix()}?mode=ro", uri=True)
    try:
        table = pick_c2c_table(list_tables(con))
        if table is None or "40030" not in table.columns:
            return []
        rows = con.execute(
            f'select "40030", count(*) c from "{table.name}" '
            f'group by "40030" order by c desc limit {int(limit)}').fetchall()
        return [(str(r[0]), int(r[1])) for r in rows if r[0] is not None]
    finally:
        con.close()
