"""低风险 QQ NT 密钥探测：扫描运行中 QQ 进程内存的可打印 16 字节串，
用 SQLCipher4 参数在真实数据库首页上做密码学校验。

不注入、不挂调试器、不修改 QQ 任何数据。
"""
import ctypes
import ctypes.wintypes as wt
import sys
import time
from concurrent.futures import ThreadPoolExecutor

from Crypto.Cipher import AES
from Crypto.Hash import SHA512

k32 = ctypes.WinDLL("kernel32", use_last_error=True)

PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ = 0x0010
MEM_COMMIT = 0x1000
PAGE_NOACCESS = 0x01
PAGE_GUARD = 0x100
READABLE = (0x02, 0x04, 0x08, 0x20, 0x40, 0x80)  # R, RW, WRITECOPY, EXEC_R, EXEC_RW, EXEC_WC


class MEMORY_BASIC_INFORMATION64(ctypes.Structure):
    _fields_ = [
        ("BaseAddress", ctypes.c_ulonglong),
        ("AllocationBase", ctypes.c_ulonglong),
        ("AllocationProtect", wt.DWORD),
        ("__alignment1", wt.DWORD),
        ("RegionSize", ctypes.c_ulonglong),
        ("State", wt.DWORD),
        ("Protect", wt.DWORD),
        ("Type", wt.DWORD),
        ("__alignment2", wt.DWORD),
    ]


k32.OpenProcess.restype = wt.HANDLE
k32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
k32.VirtualQueryEx.restype = ctypes.c_size_t
k32.VirtualQueryEx.argtypes = [wt.HANDLE, ctypes.c_void_p,
                               ctypes.POINTER(MEMORY_BASIC_INFORMATION64), ctypes.c_size_t]
k32.ReadProcessMemory.restype = wt.BOOL
k32.ReadProcessMemory.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
                                  ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]


def find_qq_pids():
    """找到 QQ.exe 且加载了 wrapper.node 的进程。"""
    import subprocess
    out = subprocess.run(
        ["tasklist", "/FI", "IMAGENAME eq QQ.exe", "/FO", "CSV", "/NH"],
        capture_output=True, text=True, encoding="gbk", errors="replace").stdout
    pids = []
    for line in out.splitlines():
        parts = [p.strip('"') for p in line.split('","')]
        if len(parts) >= 2 and parts[0].lower() == "qq.exe":
            try:
                pids.append(int(parts[1]))
            except ValueError:
                pass
    return pids


def collect_candidates(h, max_candidates, min_len=16, max_len=16):
    """扫描可读内存区域，收集长度为 min_len..max_len 的可打印 ASCII 串。"""
    cands = {}
    mbi = MEMORY_BASIC_INFORMATION64()
    addr = 0
    MAX_ADDR = 0x7FFFFFFFFFFF
    scanned = 0
    while addr < MAX_ADDR and len(cands) < max_candidates:
        if not k32.VirtualQueryEx(h, ctypes.c_void_p(addr), ctypes.byref(mbi), ctypes.sizeof(mbi)):
            addr += 0x1000
            continue
        base = mbi.BaseAddress
        size = mbi.RegionSize
        if size == 0:
            addr += 0x1000
            continue
        ok = (mbi.State == MEM_COMMIT and mbi.Protect not in (0, PAGE_NOACCESS)
              and not (mbi.Protect & PAGE_GUARD))
        if ok and size <= 64 * 1024 * 1024:
            buf = ctypes.create_string_buffer(size)
            read = ctypes.c_size_t(0)
            if k32.ReadProcessMemory(h, ctypes.c_void_p(base), buf, size, ctypes.byref(read)):
                data = buf.raw[:read.value]
                scanned += len(data)
                # 找连续可打印串
                i = 0
                n = len(data)
                while i < n:
                    b = data[i]
                    if 0x21 <= b <= 0x7E:
                        j = i
                        while j < n and 0x21 <= data[j] <= 0x7E:
                            j += 1
                        run = data[i:j]
                        if min_len <= len(run) <= max_len:
                            cands[run] = cands.get(run, 0) + 1
                        i = j
                    else:
                        i += 1
        addr = base + size
    return cands, scanned


def load_db_head(path, header=1024, page=4096):
    with open(path, "rb") as f:
        f.seek(header)
        salt = f.read(16)
        ct = f.read(page - 16)
    return salt, ct


def _hmac_salt(salt):
    return bytes([((b + 0x3A) ^ 0xFF) & 0xFF for b in salt])


def check_key(full_page, key_bytes, kdf_iter=4000, kdf_sha512=True, hmac_sha512=True):
    """校验候选密钥能否解密第 1 页。

    full_page 必须是完整的第 1 页（salt16 + 密文 + IV16 + HMAC）。
    页布局：
      第1页: [盐值16][密文][IV16][HMAC64]
    """
    import hashlib as _h
    import hmac as hmac_mod

    salt = full_page[:16]
    try:
        dk = _h.pbkdf2_hmac("sha512" if kdf_sha512 else "sha1",
                            key_bytes, salt, kdf_iter, 32)
        hkey = _h.pbkdf2_hmac("sha512" if hmac_sha512 else "sha1",
                              dk, _hmac_salt(salt), 2, 32)
    except Exception:
        return False

    mac_size = 64 if hmac_sha512 else 20
    page = len(full_page)
    ct_len = page - 16 - 16 - mac_size
    ct = full_page[16:16 + ct_len]
    iv = full_page[16 + ct_len:16 + ct_len + 16]
    mac = full_page[16 + ct_len + 16:]

    digest = hmac_mod.new(hkey, ct + iv,
                          "sha512" if hmac_sha512 else "sha1").digest()
    if not hmac_mod.compare_digest(digest, mac):
        return False

    pt = AES.new(dk, AES.MODE_CBC, iv).decrypt(ct)
    return pt[:16] == b"SQLite format 3\x00"


def check_key_quick(full_page, key_bytes, kdf_iter=4000, kdf_sha512=True,
                    hmac_sha512=True):
    """只做解密+头部比对（不做 HMAC），用于初筛。"""
    import hashlib as _h

    salt = full_page[:16]
    try:
        dk = _h.pbkdf2_hmac("sha512" if kdf_sha512 else "sha1",
                            key_bytes, salt, kdf_iter, 32)
    except Exception:
        return False
    mac_size = 64 if hmac_sha512 else 20
    page = len(full_page)
    ct_len = page - 16 - 16 - mac_size
    ct = full_page[16:16 + ct_len]
    iv = full_page[16 + ct_len:16 + ct_len + 16]
    pt = AES.new(dk, AES.MODE_CBC, iv).decrypt(ct)
    return pt[:16] == b"SQLite format 3\x00"


if __name__ == "__main__":
    db = sys.argv[1] if len(sys.argv) > 1 else \
        r"H:\path\to\文件\qq聊天数据备份\Tencent Files\1987654321\nt_qq\nt_db\nt_msg.db"
    max_c = int(sys.argv[2]) if len(sys.argv) > 2 else 20000

    extra = [int(x) for x in sys.argv[3:]] if len(sys.argv) > 3 else []
    pids = extra or find_qq_pids()
    print("QQ pids:", pids, flush=True)
    if not pids:
        sys.exit("no QQ process")

    h = k32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pids[0])
    if not h:
        sys.exit(f"OpenProcess failed: {ctypes.get_last_error()}")

    t0 = time.time()
    cands, scanned = collect_candidates(h, max_c)
    print(f"scanned {scanned/1024/1024:.0f} MB, {len(cands)} unique printable 16-char candidates, "
          f"{time.time()-t0:.1f}s", flush=True)

    salt, ct = load_db_head(db)
    print("salt:", salt.hex(), flush=True)

    combos = [
        (4000, True, True),
        (4000, True, False),
        (4000, False, False),
        (256000, True, True),
    ]

    def trial(item):
        raw = item[0]
        for it, k512, h512 in combos:
            if check_key(salt, ct, 4096, raw, it, k512, h512):
                return (raw, it, k512, h512)
        return None

    keys = list(cands.keys())
    print(f"testing {len(keys)} candidates x {len(combos)} param sets ...", flush=True)
    t0 = time.time()
    hit = None
    with ThreadPoolExecutor(max_workers=12) as ex:
        for i, res in enumerate(ex.map(trial, [(k,) for k in keys], chunksize=64)):
            if res:
                hit = res
                print("HIT:", res, flush=True)
                break
            if i and i % 2000 == 0:
                print(f"  ...{i} tested, {time.time()-t0:.0f}s", flush=True)
    print("done, %.1fs" % (time.time() - t0), flush=True)
    if hit:
        raw, it, k512, h512 = hit
        print("KEY_BYTES_HEX:", raw.hex())
        print("KEY_ASCII:", raw.decode("latin-1"))
        print("PARAMS: kdf_iter=%d kdf_sha512=%s hmac_sha512=%s" % (it, k512, h512))
    else:
        print("NO KEY FOUND in candidate set")
