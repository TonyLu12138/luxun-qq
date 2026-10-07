# -*- coding: utf-8 -*-
"""QQ NT 数据库解密（SQLCipher 4）。

自行实现 SQLCipher 4 解密，不依赖 sqlcipher3 编译包：
  - 文件前 1024 字节是 NTQQ 自定义头，需跳过
  - 盐值 = SQLCipher 数据流前 16 字节
  - 密钥 = PBKDF2(passphrase, salt, kdf_iter, 32, sha512)
  - 页内布局 = [加密体 (page-56)] [IV(16)] [HMAC(64)]
  - HMAC key = PBKDF2(数据密钥, salt^0x3a, 2, 32, sha512)
  - 加密 = AES-256-CBC，逐页

正确性由 `tests/test_crypto_roundtrip.py` 的往返自检保证。
"""
from __future__ import annotations

import hashlib
import hmac as hmac_mod
from dataclasses import dataclass
from typing import Iterator, Optional

from Crypto.Cipher import AES


class DecryptError(RuntimeError):
    """解密相关的错误。"""


DEFAULT_HEADER_BYTES = 1024
DEFAULT_PAGE_SIZE = 4096
DEFAULT_KDF_ITER = 4000


def _hmac_salt(salt: bytes) -> bytes:
    """SQLCipher 的 HMAC 盐：每字节 +0x3A 后取反。"""
    return bytes([((b + 0x3A) ^ 0xFF) & 0xFF for b in salt])


@dataclass
class CipherParams:
    """SQLCipher 参数。不同 QQ 版本可能不同，因此可配置。"""

    page_size: int = DEFAULT_PAGE_SIZE
    kdf_iter: int = DEFAULT_KDF_ITER
    kdf_sha512: bool = True
    hmac_sha512: bool = True
    header_bytes: int = DEFAULT_HEADER_BYTES

    @property
    def hmac_size(self) -> int:
        return 64 if self.hmac_sha512 else 20

    @property
    def reserve(self) -> int:
        """每页尾部保留区 = IV(16) + HMAC。"""
        return 16 + self.hmac_size

    @property
    def payload_size(self) -> int:
        """页内密文长度。

        注意：第 1 页页内还要放 16 字节盐值，所以密文比其后各页少 16 字节。
        该值固定指第 1 页的密文长度（page_size - 16盐值 - 16IV - HMAC）。
        """
        return self.page_size - 16 - self.reserve


def derive_keys(passphrase: bytes, salt: bytes, p: CipherParams) -> tuple[bytes, bytes]:
    """返回 (数据密钥, HMAC 密钥)。"""
    kdf = "sha512" if p.kdf_sha512 else "sha1"
    key = hashlib.pbkdf2_hmac(kdf, passphrase, salt, p.kdf_iter, 32)
    hmac_key = hashlib.pbkdf2_hmac("sha512" if p.hmac_sha512 else "sha1",
                                   key, _hmac_salt(salt), 2, 32)
    return key, hmac_key


def read_salt(db_path: str, p: CipherParams) -> bytes:
    """读取 SQLCipher 盐值。

    盐值是密钥派生的依据，紧跟在 1024 字节自定义头之后。
    """
    with open(db_path, "rb") as f:
        f.seek(p.header_bytes)
        salt = f.read(16)
    if len(salt) != 16:
        raise DecryptError(f"读取盐值失败：{db_path} 长度不足")
    return salt


def read_page1(db_path: str, p: CipherParams) -> bytes:
    """读取第 1 页（跳过 16 字节盐值）。

    自定义头(1024) + 盐值(16) 之后才是数据库页 1。
    """
    with open(db_path, "rb") as f:
        f.seek(p.header_bytes + 16)
        page = f.read(p.page_size)
    if len(page) != p.page_size:
        raise DecryptError(f"第 1 页不完整：读到 {len(page)} 字节")
    return page


def decrypt_page(page_bytes: bytes, key: bytes, hmac_key: bytes, p: CipherParams,
                 *, head_len: int = 0) -> bytes:
    """解密单页，返回明文（长度 = page_size - head_len - reserve）。

    页布局：
      普通页：[密文(page-80)] [IV(16)] [HMAC(64)]
      第 1 页：[明文头(16)] [密文(page-96)] [IV(16)] [HMAC(64)]

    第 1 页的页首 16 字节是明文 SQLite 头（`SQLite format 3\\0`），不参与加密，
    因此其密文比普通页少 16 字节；页尾保留区相应多 16 字节。
    HMAC 覆盖 `[密文][IV]`。
    """
    if len(page_bytes) != p.page_size:
        raise DecryptError(f"页长度异常：{len(page_bytes)} != {p.page_size}")

    ct_len = p.page_size - head_len - p.reserve
    if ct_len <= 0:
        raise DecryptError(f"页布局参数非法：head_len={head_len} reserve={p.reserve}")
    ct = page_bytes[head_len:head_len + ct_len]
    iv = page_bytes[head_len + ct_len:head_len + ct_len + 16]
    mac = page_bytes[head_len + ct_len + 16:]

    # HMAC 覆盖 [密文][IV]
    expect = hmac_mod.new(hmac_key, ct + iv,
                          "sha512" if p.hmac_sha512 else "sha1").digest()
    if not hmac_mod.compare_digest(expect, mac):
        raise DecryptError("页 HMAC 校验失败（密钥错误或页损坏）")
    return AES.new(key, AES.MODE_CBC, iv).decrypt(ct)


def decrypt_page1(db_path: str, passphrase: str | bytes,
                  p: CipherParams | None = None) -> bytes:
    """解密并返回第 1 页的密文区明文（4000 字节）。

    第 1 页的页首 16 字节是明文 SQLite 头（不经加密），密文区为其后的 4000 字节。
    """
    p = p or CipherParams()
    salt = read_salt(db_path, p)
    page = read_page1(db_path, p)
    pass_bytes = passphrase if isinstance(passphrase, bytes) else passphrase.encode("utf-8")
    key, hk = derive_keys(pass_bytes, salt, p)
    return decrypt_page(page, key, hk, p, head_len=16)


def verify_key(db_path: str, passphrase: str | bytes, p: CipherParams | None = None) -> bool:
    """校验密钥：页首必须是 SQLite 魔数，且第 1 页 HMAC 必须能被该密钥验证通过。"""
    p = p or CipherParams()
    try:
        page = read_page1(db_path, p)
        if page[:16] != b"SQLite format 3\x00":
            return False
        decrypt_page1(db_path, passphrase, p)
        return True
    except Exception:
        return False


def iter_pages(db_path: str, passphrase: str | bytes, p: CipherParams | None = None
               ) -> Iterator[tuple[int, bytes, Optional[str]]]:
    """逐页解密。产出 (页号, 明文, 错误信息)。

    文件结构：
        自定义头(1024) | 盐值(16) | 页1(4096) | 页2(4096) | ...
    页 1 的页首 16 字节是明文 SQLite 头（`SQLite format 3\\0`），不经加密；
    密文区为其后的 4000 字节。其余页密文区为 4016 字节。
    明文拼装：页 1 = 明文头(16) + 解密体(4000) + 保留区补零；
              其余页 = 解密体(4016) + 保留区补零。
    """
    p = p or CipherParams()
    pass_bytes = passphrase if isinstance(passphrase, bytes) else passphrase.encode("utf-8")
    salt = read_salt(db_path, p)
    key, hk = derive_keys(pass_bytes, salt, p)

    with open(db_path, "rb") as f:
        f.seek(p.header_bytes + 16)
        page_no = 0
        while True:
            chunk = f.read(p.page_size)
            if not chunk:
                break
            page_no += 1
            if len(chunk) < p.page_size:
                yield page_no, b"", f"末页不完整（{len(chunk)} 字节），已跳过"
                break
            try:
                head = chunk[:16] if page_no == 1 else b""
                pt = decrypt_page(chunk, key, hk, p, head_len=len(head))
                body = head + pt
                body += b"\x00" * (p.page_size - len(body))
                yield page_no, body, None
            except DecryptError as e:
                yield page_no, b"", str(e)


def decrypt_to_plaintext(db_path: str, out_path: str, passphrase: str | bytes,
                         p: CipherParams | None = None,
                         progress_every: int = 2000) -> dict:
    """解密整库并写为明文 SQLite。

    返回统计信息 dict。遇到坏页会跳过并计数（保留 SQLite 文件长度，
    用零页填充，避免页号错位导致整个库不可读）。
    """
    p = p or CipherParams()
    if not verify_key(db_path, passphrase, p):
        raise DecryptError("密钥校验失败：请确认密钥与 SQLCipher 参数正确")

    stats = {"pages": 0, "bad_pages": 0, "bytes": 0, "errors": []}
    with open(out_path, "wb") as out:
        for page_no, pt, err in iter_pages(db_path, passphrase, p):
            stats["pages"] += 1
            if err:
                stats["bad_pages"] += 1
                if len(stats["errors"]) < 50:
                    stats["errors"].append(f"页{page_no}: {err}")
                # 坏页用零填充保持偏移
                out.write(b"\x00" * p.page_size)
                continue
            out.write(pt)
            stats["bytes"] += len(pt)
            if progress_every and page_no % progress_every == 0:
                print(f"  已解密 {page_no} 页 …", flush=True)
    return stats
