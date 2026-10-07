# -*- coding: utf-8 -*-
"""长期记忆：把对话持久化，并在回复前召回相关旧内容。

设计目标（为什么不用向量库）：
    * **零依赖**：只用标准库 sqlite3。本机装第三方包很不稳定，
      而 SQLite 自带的 FTS5 已经够用。
    * **中文可检索**：SQLite 默认分词器不切中文（整句变一个 token），
      所以这里自己做**二元分词**（"今天上班" → "今天 天上 上班"），
      再交给 FTS5——这样两个汉字也能搜到。
    * **分两层记忆**：
        - 消息层：全部对话原文，用于"很久以前说过什么"的召回
        - 事实层：从对话里抽出的长期事实（生日、工作、偏好、约定），
          不加检索也总是注入，因为它们对每一轮都重要

只用 SQLite 的代价是不能按语义相似度召回（"很累" 搜不到 "疲惫"）。
对本场景够用：日常聊天里**关键词重合度**就是最强的召回信号，
而且事实层补住了语义的那部分。
"""
from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

__all__ = ["MemoryStore", "MemoryMessage", "MemoryFact", "split_tokens"]

SCHEMA_VERSION = 1

# 停用词：这些字做二元组会命中一大片，降低检索质量
_STOP_BIGRAMS = {
    "什么", "怎么", "这个", "那个", "可以", "不是", "没有", "就是", "还是",
    "但是", "因为", "所以", "如果", "我们", "你们", "他们", "自己", "一个",
    "现在", "已经", "今天", "明天", "昨天", "时候", "东西", "事情", "一下",
}


def _chinese_bigrams(text: str) -> list[str]:
    """把中文串切成二元组。

    "今天上班好累" -> ["今天","天上","上班","班好","好累"]

    为什么不切单字：单字（"好"、"上"）命中率过高，检索会退化成噪声。
    二元组在中文检索里是性价比最高的粒度，也是搜索引擎的常用做法。
    """
    out: list[str] = []
    for run in re.findall(r"[\u4e00-\u9fff]+", text):
        if len(run) == 1:
            out.append(run)
            continue
        for i in range(len(run) - 1):
            out.append(run[i:i + 2])
    return out


def _ascii_words(text: str) -> list[str]:
    """英文单词与数字（小写化）。"""
    return [w.lower() for w in re.findall(r"[A-Za-z0-9_]{2,}", text)]


def split_tokens(text: str, *, drop_stop: bool = True) -> list[str]:
    """把一段文本切成用于检索的 token 列表（中文二元组 + 英文词）。"""
    toks = _chinese_bigrams(text) + _ascii_words(text)
    if drop_stop:
        toks = [t for t in toks if t not in _STOP_BIGRAMS]
    # 去重但保序，避免超长文本把 FTS 查询撑爆
    seen: set[str] = set()
    out: list[str] = []
    for t in toks:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


def _fts_query(text: str, *, max_terms: int = 24) -> str:
    """把一段文本转成 FTS5 查询串（OR 连接）。

    用 OR 而不是 AND：日常召回宁可多带几条相关的，
    漏掉关键旧信息比多塞两条噪声代价更大。
    """
    toks = split_tokens(text)[:max_terms]
    if not toks:
        return ""
    # 加引号防止 FTS 语法字符（如 * - :）被当运算符
    return " OR ".join('"' + t.replace('"', '') + '"' for t in toks)


@dataclass
class MemoryMessage:
    ts: float
    mine: bool
    text: str
    row_id: int = 0

    @property
    def who(self) -> str:
        return "我" if self.mine else "对方"

    def when(self) -> str:
        return time.strftime("%m-%d %H:%M", time.localtime(self.ts))

    def line(self) -> str:
        return f"[{self.when()}] {self.who}：{self.text}"


@dataclass
class MemoryFact:
    kind: str
    content: str
    ts: float = 0.0
    hits: int = 0
    row_id: int = 0

    def line(self) -> str:
        return f"（{self.kind}）{self.content}"


class MemoryStore:
    """SQLite 长期记忆库。线程安全（sqlite3 连接加锁复用）。"""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._recovered = False
        self._con = self._connect()
        self._prepare()

    def _prepare(self) -> None:
        """建表 + 必要时自愈，对"库被别的连接占用"做重试。

        为什么需要重试：``busy_timeout`` 只能覆盖"等锁"的情况，覆盖不了
        SQLITE_BUSY 的死锁场景（读事务想升级为写事务、而别人正持写锁）。
        多进程同时打开同一个库（服务进程 + 你在另一个终端跑 ``memory``
        CLI）时就会命中，于是这里退化成"记忆不可用"。
        """
        last: Optional[Exception] = None
        for attempt in range(5):
            try:
                if not self._integrity_ok():
                    # 库损坏时不能让整个服务起不来：改名留档后重建。
                    self._recover()
                self._init_schema()
                return
            except sqlite3.OperationalError as e:
                if "locked" not in str(e).lower() and "busy" not in str(e).lower():
                    raise
                last = e
                time.sleep(0.3 * (attempt + 1))
        raise last if last else RuntimeError("记忆库初始化失败")

    def _connect(self) -> sqlite3.Connection:
        """建连接并**立刻**设 busy_timeout。

        顺序很关键：超时必须早于任何会取锁的操作（journal_mode、建表、
        quick_check）。否则多进程访问（服务 + CLI + 测试）时会直接抛
        "database is locked"，被上层当成"记忆不可用"而降级。
        """
        con = sqlite3.connect(str(self.path), check_same_thread=False,
                              timeout=10.0)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA busy_timeout=10000")
        # WAL 只在首次生效，后续调用是空操作；放在超时之后设。
        # 这里必须捕获 DatabaseError（不只 OperationalError）：对垃圾文件
        # 执行 journal_mode 会抛 "file is not a database"，那是 DatabaseError，
        # 不能让它穿透出去——否则损坏的库会直接把服务初始化打挂，
        # 后面的 _integrity_ok + _recover 也就没机会自愈。
        try:
            con.execute("PRAGMA journal_mode=WAL")
        except sqlite3.DatabaseError:
            pass
        return con

    def _integrity_ok(self) -> bool:
        """检查数据库是否**真的损坏**。

        这里必须区分两种失败，否则会造成灾难性误判：

        * ``OperationalError("database is locked")`` —— 只是别的连接（比如
          服务进程 + 你在另一个终端跑 ``memory`` CLI）正在用，**不是损坏**。
          早期版本把 ``DatabaseError`` 一概当成损坏，结果服务运行期间打开
          CLI 就会把整份记忆库当坏库改名重建，等于**静默清空用户记忆**。
        * ``DatabaseError("file is not a database" / "malformed")`` —— 真损坏。

        注意 OperationalError 是 DatabaseError 的子类，捕获顺序不能颠倒。
        """
        try:
            row = self._con.execute("PRAGMA quick_check(1)").fetchone()
        except sqlite3.OperationalError:
            return True          # 忙/锁：当作可用，别动它
        except sqlite3.DatabaseError:
            return False         # malformed / not a database
        return bool(row) and str(row[0]).lower() == "ok"

    def _recover(self) -> None:
        """把损坏的库改名留档，然后建一个干净的新库。"""
        try:
            self._con.close()
        except Exception:
            pass
        stamp = time.strftime("%Y%m%d-%H%M%S")
        broken = self.path.with_suffix(self.path.suffix + f".broken-{stamp}")
        try:
            self.path.replace(broken)
        except OSError:
            try:
                self.path.unlink()
            except OSError:
                pass
        # WAL/SHM 也要一起挪走，否则新库会继承坏状态
        for suffix in ("-wal", "-shm"):
            side = Path(str(self.path) + suffix)
            if side.exists():
                try:
                    side.replace(Path(str(broken) + suffix))
                except OSError:
                    try:
                        side.unlink()
                    except OSError:
                        pass
        self._con = self._connect()
        self._recovered = True
        self.broken_backup = broken

    # ------------------------------------------------------------ 建表
    def _init_schema(self) -> None:
        with self._lock:
            c = self._con
            c.execute("PRAGMA synchronous=NORMAL")
            c.execute("PRAGMA busy_timeout=10000")
            c.executescript("""
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT
                );

                -- 全部对话原文（长期）
                -- tokens 存"分词后的文本"，供 FTS5 检索。
                -- 放在内容表里（而不是 FTS 表里）是 FTS5 external content
                -- 的标准用法：删除/更新时触发器能拿到旧值，索引才不会残留。
                CREATE TABLE IF NOT EXISTS messages (
                    id      INTEGER PRIMARY KEY AUTOINCREMENT,
                    peer_id TEXT NOT NULL,
                    ts      REAL NOT NULL,
                    mine    INTEGER NOT NULL,
                    text    TEXT NOT NULL,
                    msg_id  TEXT,
                    tokens  TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_msg_peer_ts
                    ON messages(peer_id, ts DESC);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_msg_dedup
                    ON messages(peer_id, msg_id)
                    WHERE msg_id IS NOT NULL AND msg_id != '';

                -- 从对话里抽出的长期事实
                CREATE TABLE IF NOT EXISTS facts (
                    id      INTEGER PRIMARY KEY AUTOINCREMENT,
                    peer_id TEXT NOT NULL,
                    kind    TEXT NOT NULL DEFAULT '其他',
                    content TEXT NOT NULL,
                    ts      REAL NOT NULL,
                    hits    INTEGER NOT NULL DEFAULT 0,
                    tokens  TEXT NOT NULL DEFAULT '',
                    UNIQUE(peer_id, content)
                );
                CREATE INDEX IF NOT EXISTS idx_fact_peer
                    ON facts(peer_id, ts DESC);

                -- QQ 表情 id -> 名称 的自学习缓存
                CREATE TABLE IF NOT EXISTS face_names (
                    face_id TEXT PRIMARY KEY,
                    name    TEXT NOT NULL,
                    seen    INTEGER NOT NULL DEFAULT 1,
                    ts      REAL NOT NULL
                );
            """)

            # 老库升级：补 tokens 列
            for table in ("messages", "facts"):
                if "tokens" not in self._columns(table):
                    c.execute(f"ALTER TABLE {table} ADD COLUMN tokens TEXT DEFAULT ''")

            # 全文索引（external content）
            c.executescript("""
                CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts
                    USING fts5(tokens, content='messages', content_rowid='id');
                CREATE VIRTUAL TABLE IF NOT EXISTS facts_fts
                    USING fts5(tokens, content='facts', content_rowid='id');
            """)

            # 触发器保持索引同步：增删改都由 SQLite 负责，
            # Python 侧只需要写好 messages.tokens 即可。
            c.executescript("""
                CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
                    INSERT INTO messages_fts(rowid, tokens)
                    VALUES (new.id, new.tokens);
                END;
                CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
                    INSERT INTO messages_fts(messages_fts, rowid, tokens)
                    VALUES ('delete', old.id, old.tokens);
                END;
                CREATE TRIGGER IF NOT EXISTS facts_ai AFTER INSERT ON facts BEGIN
                    INSERT INTO facts_fts(rowid, tokens)
                    VALUES (new.id, new.tokens);
                END;
                CREATE TRIGGER IF NOT EXISTS facts_ad AFTER DELETE ON facts BEGIN
                    INSERT INTO facts_fts(facts_fts, rowid, tokens)
                    VALUES ('delete', old.id, old.tokens);
                END;
            """)

            c.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)",
                      (str(SCHEMA_VERSION),))
            c.commit()

    def _columns(self, table: str) -> set[str]:
        return {r["name"] for r in self._con.execute(f"PRAGMA table_info({table})")}

    def rebuild_index(self) -> None:
        """按内容表重建全文索引（换了分词规则或老库升级后可用）。"""
        with self._lock:
            self._con.execute("INSERT INTO messages_fts(messages_fts) VALUES('rebuild')")
            self._con.execute("INSERT INTO facts_fts(facts_fts) VALUES('rebuild')")
            self._con.commit()

    # ------------------------------------------------------------ 写入：消息
    def add_message(self, peer_id: str, *, mine: bool, text: str,
                    msg_id: str = "", ts: Optional[float] = None) -> bool:
        """存一条消息。返回 False 表示重复（同一 msg_id 已存在）。

        全文索引由触发器同步，这里只负责把 tokens 算好写进内容表。
        """
        text = (text or "").strip()
        if not text:
            return False
        ts = float(ts if ts is not None else time.time())
        toks = " ".join(split_tokens(text))
        with self._lock:
            try:
                self._con.execute(
                    "INSERT INTO messages(peer_id, ts, mine, text, msg_id, tokens) "
                    "VALUES(?,?,?,?,?,?)",
                    (str(peer_id), ts, 1 if mine else 0, text, msg_id or "", toks))
            except sqlite3.IntegrityError:
                return False
            self._con.commit()
            return True

    # ------------------------------------------------------------ 写入：事实
    def add_fact(self, peer_id: str, kind: str, content: str,
                 ts: Optional[float] = None) -> bool:
        """存一条事实。返回 False 表示已存在（按内容去重）。"""
        content = (content or "").strip()
        if not content:
            return False
        ts = float(ts if ts is not None else time.time())
        toks = " ".join(split_tokens(content + " " + (kind or "")))
        with self._lock:
            try:
                self._con.execute(
                    "INSERT INTO facts(peer_id, kind, content, ts, tokens) "
                    "VALUES(?,?,?,?,?)",
                    (str(peer_id), kind or "其他", content, ts, toks))
            except sqlite3.IntegrityError:
                # 已存在：把时间戳刷新为最近一次提及
                self._con.execute(
                    "UPDATE facts SET ts=? WHERE peer_id=? AND content=?",
                    (ts, str(peer_id), content))
                self._con.commit()
                return False
            self._con.commit()
            return True

    # ------------------------------------------------------------ 读取
    def recent(self, peer_id: str, limit: int = 20) -> list[MemoryMessage]:
        """最近 limit 条消息，返回**时间正序**（老的在前）。"""
        with self._lock:
            rows = self._con.execute(
                "SELECT id, ts, mine, text FROM messages WHERE peer_id=? "
                "ORDER BY ts DESC, id DESC LIMIT ?",
                (str(peer_id), int(limit))).fetchall()
        msgs = [MemoryMessage(ts=r["ts"], mine=bool(r["mine"]),
                              text=r["text"], row_id=r["id"]) for r in rows]
        msgs.reverse()
        return msgs

    def recall(self, peer_id: str, query: str, *, limit: int = 8,
               before_ts: Optional[float] = None,
               exclude_ids: Optional[Iterable[int]] = None
               ) -> list[MemoryMessage]:
        """按关键词召回**较早**的相关消息（长期记忆的核心）。

        Args:
            before_ts: 只召回这个时间之前的（用来避开"最近窗口"里已有的内容）
            exclude_ids: 明确排除的行号
        """
        fts = _fts_query(query)
        if not fts:
            return []
        sql = ("SELECT m.id, m.ts, m.mine, m.text, bm25(messages_fts) AS rk "
               "FROM messages_fts JOIN messages m ON m.id = messages_fts.rowid "
               "WHERE messages_fts MATCH ? AND m.peer_id = ?")
        params: list[Any] = [fts, str(peer_id)]
        if before_ts is not None:
            sql += " AND m.ts < ?"
            params.append(float(before_ts))
        sql += " ORDER BY rk LIMIT ?"
        params.append(int(limit) * 3)          # 多取一些，过滤后再截断

        with self._lock:
            try:
                rows = self._con.execute(sql, params).fetchall()
            except sqlite3.OperationalError:
                # FTS 语法异常时不要让整轮回复失败
                return []
        skip = set(exclude_ids or ())
        out: list[MemoryMessage] = []
        for r in rows:
            if r["id"] in skip:
                continue
            out.append(MemoryMessage(ts=r["ts"], mine=bool(r["mine"]),
                                     text=r["text"], row_id=r["id"]))
            if len(out) >= limit:
                break
        out.reverse()                          # 展示时按时间正序更自然
        return out

    def facts(self, peer_id: str, *, limit: int = 40,
              kinds: Optional[Iterable[str]] = None) -> list[MemoryFact]:
        """长期事实，最近的在前。"""
        sql = "SELECT id, kind, content, ts, hits FROM facts WHERE peer_id=?"
        params: list[Any] = [str(peer_id)]
        if kinds:
            ks = list(kinds)
            sql += " AND kind IN (" + ",".join("?" * len(ks)) + ")"
            params.extend(ks)
        sql += " ORDER BY ts DESC LIMIT ?"
        params.append(int(limit))
        with self._lock:
            rows = self._con.execute(sql, params).fetchall()
        return [MemoryFact(kind=r["kind"], content=r["content"], ts=r["ts"],
                           hits=r["hits"], row_id=r["id"]) for r in rows]

    def recall_facts(self, peer_id: str, query: str,
                     *, limit: int = 6) -> list[MemoryFact]:
        """按关键词召回相关事实（除了总是注入的最近事实之外）。"""
        fts = _fts_query(query)
        if not fts:
            return []
        with self._lock:
            try:
                rows = self._con.execute(
                    "SELECT f.id, f.kind, f.content, f.ts, f.hits, "
                    "bm25(facts_fts) AS rk "
                    "FROM facts_fts JOIN facts f ON f.id = facts_fts.rowid "
                    "WHERE facts_fts MATCH ? AND f.peer_id = ? "
                    "ORDER BY rk LIMIT ?",
                    (fts, str(peer_id), int(limit))).fetchall()
            except sqlite3.OperationalError:
                return []
        return [MemoryFact(kind=r["kind"], content=r["content"], ts=r["ts"],
                           hits=r["hits"], row_id=r["id"]) for r in rows]

    def bump_fact_hits(self, fact_ids: Iterable[int]) -> None:
        ids = [int(i) for i in fact_ids]
        if not ids:
            return
        with self._lock:
            self._con.executemany(
                "UPDATE facts SET hits = hits + 1 WHERE id = ?",
                [(i,) for i in ids])
            self._con.commit()

    # ------------------------------------------------------------ 表情名缓存
    def learn_face(self, face_id: str, name: str) -> None:
        """记下某个 QQ 表情 id 的名称（自学习，越用越全）。"""
        fid, nm = str(face_id or "").strip(), (name or "").strip()
        if not fid or not nm:
            return
        with self._lock:
            self._con.execute(
                "INSERT INTO face_names(face_id, name, seen, ts) VALUES(?,?,1,?) "
                "ON CONFLICT(face_id) DO UPDATE SET "
                "seen = seen + 1, name = excluded.name, ts = excluded.ts",
                (fid, nm, time.time()))
            self._con.commit()

    def face_names(self) -> dict[str, str]:
        with self._lock:
            rows = self._con.execute(
                "SELECT face_id, name FROM face_names").fetchall()
        return {r["face_id"]: r["name"] for r in rows}

    # ------------------------------------------------------------ 统计与维护
    def stats(self, peer_id: Optional[str] = None) -> dict[str, Any]:
        with self._lock:
            if peer_id:
                m = self._con.execute(
                    "SELECT COUNT(*) c, MIN(ts) a, MAX(ts) b FROM messages "
                    "WHERE peer_id=?", (str(peer_id),)).fetchone()
                f = self._con.execute(
                    "SELECT COUNT(*) c FROM facts WHERE peer_id=?",
                    (str(peer_id),)).fetchone()
            else:
                m = self._con.execute(
                    "SELECT COUNT(*) c, MIN(ts) a, MAX(ts) b FROM messages").fetchone()
                f = self._con.execute("SELECT COUNT(*) c FROM facts").fetchone()
        return {
            "messages": m["c"] if m else 0,
            "facts": f["c"] if f else 0,
            "earliest": m["a"] if m else None,
            "latest": m["b"] if m else None,
            "db": str(self.path),
            "recovered": self._recovered,
            "size_kb": round(self.path.stat().st_size / 1024, 1)
            if self.path.exists() else 0.0,
        }

    def peers(self) -> list[dict[str, Any]]:
        """所有会话对象及各自消息数（按最近活跃排序）。"""
        with self._lock:
            rows = self._con.execute(
                "SELECT peer_id, COUNT(*) c, MAX(ts) last FROM messages "
                "GROUP BY peer_id ORDER BY last DESC").fetchall()
        return [{"peer_id": r["peer_id"], "messages": r["c"], "last": r["last"]}
                for r in rows]

    def forget(self, peer_id: str) -> dict[str, int]:
        """清掉某个对象的全部记忆（消息 + 事实）。

        全文索引由 DELETE 触发器自动清理，这里直接删内容表即可。
        """
        with self._lock:
            n_msg = self._con.execute(
                "SELECT COUNT(*) c FROM messages WHERE peer_id=?",
                (str(peer_id),)).fetchone()["c"]
            n_fact = self._con.execute(
                "SELECT COUNT(*) c FROM facts WHERE peer_id=?",
                (str(peer_id),)).fetchone()["c"]
            self._con.execute("DELETE FROM messages WHERE peer_id=?", (str(peer_id),))
            self._con.execute("DELETE FROM facts WHERE peer_id=?", (str(peer_id),))
            self._con.commit()
        return {"messages": n_msg, "facts": n_fact}

    def close(self) -> None:
        with self._lock:
            try:
                self._con.commit()
                self._con.close()
            except Exception:
                pass
