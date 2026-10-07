# -*- coding: utf-8 -*-
"""带重试与进度显示的下载器（用 Python urllib，绕开本机 PowerShell/curl 的 TLS 故障）。

用法：
    python tools/download.py <url> <out_path> [--retries 3]
"""
from __future__ import annotations

import argparse
import os
import sys
import time
import urllib.request

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


def download(url: str, out: str, retries: int = 3, chunk: int = 1 << 20) -> bool:
    os.makedirs(os.path.dirname(os.path.abspath(out)) or ".", exist_ok=True)
    for attempt in range(1, retries + 1):
        tmp = out + ".part"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=60) as r:
                total = int(r.headers.get("Content-Length") or 0)
                print(f"[{attempt}/{retries}] HTTP {r.status} "
                      f"size={human(total) if total else '未知'}", flush=True)
                done = 0
                t0 = time.time()
                last = 0.0
                with open(tmp, "wb") as f:
                    while True:
                        buf = r.read(chunk)
                        if not buf:
                            break
                        f.write(buf)
                        done += len(buf)
                        now = time.time()
                        if now - last > 2:
                            last = now
                            speed = done / max(now - t0, 0.001)
                            pct = f"{done * 100 / total:.1f}%" if total else "?"
                            print(f"  {pct}  {human(done)}  {human(speed)}/s",
                                  flush=True)
            os.replace(tmp, out)
            print(f"完成：{out}（{human(os.path.getsize(out))}）", flush=True)
            return True
        except Exception as e:
            print(f"  失败：{type(e).__name__}: {str(e)[:200]}", flush=True)
            if os.path.exists(tmp):
                os.remove(tmp)
            if attempt < retries:
                time.sleep(3 * attempt)
    return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("out")
    ap.add_argument("--retries", type=int, default=3)
    a = ap.parse_args()
    return 0 if download(a.url, a.out, a.retries) else 1


if __name__ == "__main__":
    raise SystemExit(main())
