"""后台持续监控 QQ 进程内存中的 SQLCipher 密钥。

思路：QQ 在建立数据库连接时会调用 sqlite3_key_v2，此刻 passphrase 理应在内存中。
本脚本周期性地扫描可打印字符串，并用 SQLCipher 参数在真实数据库首页上做校验，
一旦命中就写入 key.txt。

不注入、不调试、不修改目标进程。

用法：
    python tools/watch_qq_memory_key.py [--db <nt_msg.db>] [--out key.txt] [--rounds 60]

实测结论（2026-09，QQ 9.9.x / Windows）：
    连续扫描 5 个 QQ 进程、每轮约 3.2 万个候选，8 轮未命中。
    说明该密钥不以明文常驻内存，此路大概率走不通——
    请改用调试挂载方案（QQBackup/qq-win-db-key）。
"""
import argparse
import ctypes
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from scan_qq_memory_key import (MEMORY_BASIC_INFORMATION64, MEM_COMMIT,  # noqa: E402
                                PAGE_GUARD, PAGE_NOACCESS, k32)

DEFAULT_DB = (r"H:\path\to\文件\qq聊天数据备份\Tencent Files\1987654321"
              r"\nt_qq\nt_db\nt_msg.db")


def iter_regions(h):
    mbi = MEMORY_BASIC_INFORMATION64()
    addr = 0
    while addr < 0x7FFFFFFFFFFF:
        if not k32.VirtualQueryEx(h, ctypes.c_void_p(addr), ctypes.byref(mbi), ctypes.sizeof(mbi)):
            addr += 0x1000
            continue
        base, size = mbi.BaseAddress, mbi.RegionSize
        if size == 0:
            addr += 0x1000
            continue
        if (mbi.State == MEM_COMMIT and mbi.Protect not in (0, PAGE_NOACCESS)
                and not (mbi.Protect & PAGE_GUARD) and size <= 32 * 1024 * 1024):
            yield base, size
        addr = base + size


def scan_once(h, budget=400000):
    """只收集“恰好 16 字符”的独立可打印串（高质量候选）。"""
    cands = set()

    def add(bs):
        if len(bs) == 16 and len(cands) < budget:
            cands.add(bs)

    total = 0
    for base, size in iter_regions(h):
        buf = ctypes.create_string_buffer(size)
        read = ctypes.c_size_t(0)
        if not k32.ReadProcessMemory(h, ctypes.c_void_p(base), buf, size, ctypes.byref(read)):
            continue
        data = buf.raw[:read.value]
        total += len(data)
        n = len(data)
        i = 0
        while i < n:
            if 0x21 <= data[i] <= 0x7E:
                j = i
                while j < n and 0x21 <= data[j] <= 0x7E:
                    j += 1
                if j - i == 16:
                    add(bytes(data[i:j]))
                i = j
            else:
                i += 1
        if len(cands) >= budget:
            break
    return cands, total


def validate(cands, passphrase_probe):
    """用主项目的解密实现校验候选密钥（比自写 HMAC 更可靠）。"""
    def trial(raw):
        try:
            if passphrase_probe(raw.decode("latin-1")):
                return raw
        except Exception:
            return None
        return None

    with ThreadPoolExecutor(max_workers=12) as ex:
        for res in ex.map(trial, [(c,) for c in cands], chunksize=128):
            if res:
                return res
    return None


def _make_probe(db_path):
    """构造一个「候选密钥 -> 是否可解开第 1 页」的函数。"""
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                    "..", "src"))
    from luxun_qq.qqdb import crypto

    def probe(passphrase: str) -> bool:
        for p in (crypto.CipherParams(),):
            if crypto.verify_key(db_path, passphrase, p):
                return True
        return False

    return probe


if __name__ == "__main__":
    import subprocess

    ap = argparse.ArgumentParser(description="扫描 QQ 进程内存寻找 SQLCipher 密钥")
    ap.add_argument("--db", default=DEFAULT_DB, help="nt_msg.db 路径")
    ap.add_argument("--out", default="key.txt", help="命中的密钥写入路径")
    ap.add_argument("--rounds", type=int, default=60, help="扫描轮数")
    ap.add_argument("--interval", type=float, default=20, help="轮间隔秒数")
    args = ap.parse_args()

    if not os.path.exists(args.db):
        raise SystemExit(f"数据库不存在：{args.db}")
    probe = _make_probe(args.db)
    print(f"目标库：{args.db}", flush=True)

    for round_no in range(1, args.rounds + 1):
        try:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "(Get-Process -Name QQ -ErrorAction SilentlyContinue).Id"],
                capture_output=True, text=True).stdout
            pids = [int(x) for x in out.split()]
        except Exception:
            pids = []

        allc: set = set()
        for pid in pids:
            h = k32.OpenProcess(0x0400 | 0x0010, False, pid)
            if not h:
                continue
            c, _total = scan_once(h)
            allc |= c

        hit = validate(allc, probe) if allc else None
        print(f"round {round_no}: pids={pids} cands={len(allc)} hit={bool(hit)}",
              flush=True)
        if hit:
            key = hit.decode("latin-1")
            with open(args.out, "w", encoding="utf-8") as f:
                f.write(key)
            print("KEY_FOUND:", key, flush=True)
            print(f"已写入：{args.out}", flush=True)
            break
        time.sleep(args.interval)
    else:
        print("未命中。该密钥很可能不以明文驻留内存，请改用调试挂载方案。", flush=True)
