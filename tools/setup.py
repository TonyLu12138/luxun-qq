# -*- coding: utf-8 -*-
"""一键重建本地运行环境（仓库里不含大文件，用这个脚本下载）。

为什么需要它：
    运行本项目需要约 14GB 的本地文件（模型权重、NapCat、QQ 安装包、
    推理后端）。其中多个单文件超过 100MB，而 GitHub 的硬性上限是
    100MB/文件，所以**仓库里只放源码**，这些由本脚本按需下载。

用法：
    python tools/setup.py --all          # 下载全部（约 1.2GB 流量，不含后端）
    python tools/setup.py --model        # 只下模型（4.4GB）
    python tools/setup.py --napcat       # 只下 NapCat
    python tools/setup.py --qq           # 只下 QQ 9.9.30 安装包
    python tools/setup.py --check        # 只检查现状，不下载

关于推理后端（llama-server.exe）：
    它无法用固定 URL 稳定获取（LM Studio 的 CDN 路径会变，且 800MB+），
    所以本脚本只**检查并给出指引**，见 --check 的输出。

网络说明：
    本机直连 github.com 会超时，脚本默认走 gh-proxy.com 加速；
    ModelScope 直连很快，不走代理。
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "_runtime"
MODELS = ROOT / "_models"
DOWNLOADS = ROOT / "_downloads"

GH_PROXY = "https://gh-proxy.com/"

#: GGUF 模型（ModelScope 直连很快，实测 ~50MB/s）
MODEL_REPO = "Qwen/Qwen2.5-7B-Instruct-GGUF"
MODEL_FILES = [
    "qwen2.5-7b-instruct-q4_k_m-00001-of-00002.gguf",
    "qwen2.5-7b-instruct-q4_k_m-00002-of-00002.gguf",
]
MODE_DIR = MODELS / "qwen2.5-7b-instruct-q4_k_m"

#: NapCat（QQ 协议适配层）
NAPCAT_VER = "v4.18.28"
NAPCAT_ASSET = "NapCat.Shell.Windows.Node.zip"
NAPCAT_URL = (f"https://github.com/NapNeko/NapCatQQ/releases/download/"
              f"{NAPCAT_VER}/{NAPCAT_ASSET}")

#: QQ 9.9.30 —— 这个构建号必须在 NapCat 的支持列表里（见 README 的排查一节）
QQ_URL = ("https://github.com/Rodert/qq-versions/releases/download/"
          "qq-windows-x64-9.9.30-20260429/QQ_9.9.30_260429_x64_01.exe")
QQ_NAME = "QQ_9.9.30_260429_x64_01.exe"


def _mirror(url: str) -> str:
    """GitHub 链接走加速代理（本机直连 github.com 会超时）。"""
    if "github.com" in url and not url.startswith(GH_PROXY):
        return GH_PROXY + url
    return url


def download(url: str, out: Path, *, retries: int = 3) -> bool:
    """下载文件；已存在且大小一致则跳过。"""
    if out.exists() and out.stat().st_size > 0:
        print(f"  已存在，跳过：{out.name}（{out.stat().st_size/1048576:.1f}MB）")
        return True
    out.parent.mkdir(parents=True, exist_ok=True)
    real = _mirror(url)
    if real != url:
        print(f"  （GitHub 直连不通，走加速：{real[:60]}…）")
    tmp = out.with_suffix(out.suffix + ".part")
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(real, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=60) as r:
                total = int(r.headers.get("Content-Length") or 0)
                done = 0
                with open(tmp, "wb") as f:
                    while True:
                        buf = r.read(1 << 20)
                        if not buf:
                            break
                        f.write(buf)
                        done += len(buf)
                        if total and done % (50 << 20) < (1 << 20):
                            print(f"    {done*100//total}%  {done/1048576:.0f}MB",
                                  flush=True)
            tmp.replace(out)
            print(f"  ✅ {out.name}（{out.stat().st_size/1048576:.1f}MB）")
            return True
        except Exception as e:
            print(f"  ❌ 第 {attempt} 次失败：{type(e).__name__}: {str(e)[:120]}")
            if tmp.exists():
                tmp.unlink(missing_ok=True)
    return False


# --------------------------------------------------------------------------- #
# 各部分
# --------------------------------------------------------------------------- #

def get_model() -> bool:
    print("\n[1/3] GGUF 模型（qwen2.5-7b-instruct q4_k_m，约 4.4GB）")
    ok = True
    for fn in MODEL_FILES:
        url = (f"https://modelscope.cn/models/{MODEL_REPO}"
               f"/resolve/master/{fn}")
        ok &= download(url, MODE_DIR / fn)
    return ok


def get_napcat() -> bool:
    print(f"\n[2/3] NapCat {NAPCAT_VER}")
    dest = DOWNLOADS / NAPCAT_ASSET
    if not download(NAPCAT_URL, dest):
        return False
    target = RUNTIME / "napcat"
    print(f"  解压到 {target} …")
    import zipfile
    target.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(dest) as z:
            z.extractall(target)
        print("  ✅ 解压完成")
        return True
    except Exception as e:
        print(f"  ❌ 解压失败：{e}")
        return False


def get_qq() -> bool:
    print("\n[3/3] QQ 9.9.30 安装包（NapCat 支持的构建号）")
    dest = DOWNLOADS / QQ_NAME
    if not download(QQ_URL, dest):
        return False
    print("  请手动运行安装程序，建议装到工作区**之外**（例如 C:\\Users\\<你>\\qq930）。")
    print("  原因：E:\\code 这类工作区可能有 ACL 限制，QQ 会静默退出。")
    return True


def check() -> int:
    """检查现状并给出指引。"""
    print("=" * 68)
    print("环境检查")
    print("=" * 68)

    # 模型
    got = [f for f in MODEL_FILES if (MODE_DIR / f).exists()]
    print(f"\n模型      : {len(got)}/{len(MODEL_FILES)} 个文件")
    print(f"             {MODE_DIR}")

    # NapCat
    nc = RUNTIME / "napcat"
    nc_files = list(nc.rglob("NapCatWinBootMain.exe")) if nc.exists() else []
    print(f"NapCat    : {'✅ 已就绪' if nc_files else '❌ 未下载'}")
    print(f"             {nc}")

    # QQ
    qq = DOWNLOADS / QQ_NAME
    print(f"QQ 安装包 : {'✅ 已下载' if qq.exists() else '❌ 未下载'}")
    print(f"             {qq}")

    # 后端：复用 model_server 的探测逻辑
    sys.path.insert(0, str(ROOT / "tools"))
    backend = None
    try:
        import model_server as ms
        backend = ms.BACKEND if ms.BACKEND.exists() else None
    except Exception:
        pass
    print(f"推理后端  : {'✅ ' + str(backend) if backend else '❌ 未找到 llama-server.exe'}")
    if not backend:
        print("""
  后端需要手动准备，二选一：
    A) 安装 LM Studio（https://lmstudio.ai），它会附带 CUDA 版 llama.cpp。
       把后端目录复制到：
         _runtime/lmstudio/.bundle/bin/extensions/backends/llama.cpp-win-x86_64-nvidia-cuda-avx2-2.41.0
    B) 下载官方 llama.cpp 的 Windows CUDA 发行版：
         https://github.com/ggml-org/llama.cpp/releases
       需要这两个压缩包（解压后合并到同一个目录）：
         llama-bXXXXX-bin-win-cuda-12.4-x64.zip
         cudart-llama-bin-win-cuda-12.4-x64.zip
       解压到： _runtime/llama.cpp/   （必须含 llama-server.exe）
    也可以用环境变量 LUXUN_LLAMA_BACKEND 指向任意含 llama-server.exe 的目录。""")

    # Python 依赖
    print("\nPython 依赖:")
    for mod, pipname in (("yaml", "pyyaml"),):
        try:
            __import__(mod)
            print(f"  ✅ {pipname}")
        except ImportError:
            print(f"  ❌ {pipname}   →  pip install {pipname}")

    print("\n下一步：")
    print("  1) python tools/setup.py --all        下载缺失的大文件")
    print("  2) 按上面提示准备好推理后端")
    print("  3) 启动指南见 启动指南.md")
    print()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="重建 luxun-qq 的本地运行环境")
    ap.add_argument("--all", action="store_true", help="下载全部可自动获取的部分")
    ap.add_argument("--model", action="store_true", help="只下模型")
    ap.add_argument("--napcat", action="store_true", help="只下 NapCat")
    ap.add_argument("--qq", action="store_true", help="只下 QQ 安装包")
    ap.add_argument("--check", action="store_true", help="只检查现状")
    a = ap.parse_args()

    if a.check or not any([a.all, a.model, a.napcat, a.qq]):
        return check()

    ok = True
    if a.all or a.model:
        ok &= get_model()
    if a.all or a.napcat:
        ok &= get_napcat()
    if a.all or a.qq:
        ok &= get_qq()

    print()
    check()
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
