# -*- coding: utf-8 -*-
"""探测 QQ 官方 CDN 的可用下载地址，确认能否拿到 NapCat 兼容的 QQ 版本。"""
import json
import urllib.request

UA = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
    "Referer": "https://im.qq.com/",
    "Accept": "*/*",
    "Accept-Language": "zh-CN,zh;q=0.9",
}


def probe(url: str) -> str:
    try:
        req = urllib.request.Request(url, headers={**UA, "Range": "bytes=0-2047"})
        with urllib.request.urlopen(req, timeout=25) as r:
            cr = r.headers.get("Content-Range", "")
            total = int(cr.split("/")[-1]) / 1048576 if "/" in cr else 0
            magic = r.read(2)
            return f"OK {total:7.1f}MB magic={magic!r}"
    except urllib.error.HTTPError as e:
        return f"HTTP {e.code}"
    except Exception as e:
        return f"{type(e).__name__}"


def main() -> None:
    print("=== 1) 当前 pcConfig 报的 QQ 版本与新 CDN 直链 ===")
    cfg = json.loads(urllib.request.urlopen(urllib.request.Request(
        "https://cdn-go.cn/qq-web/im.qq.com_new/latest/rainbow/pcConfig.json",
        headers=UA), timeout=40).read().decode("utf-8", "ignore"))
    w = cfg["Windows"]
    print(f"  当前 QQ 版本: {w['version']}  (更新日期 {w.get('updateDate')})")
    for k in ("ntDownloadX64Url", "ntDownloadUrl"):
        u = w[k]
        print(f"  {k}: {probe(u)}")
        print(f"     {u}")

    print("\n=== 2) 旧 CDN 域名是否仍可用（dldir1）===")
    for u in (
        "https://dldir1.qq.com/qqfile/qq/QQNT/be71d851/QQ9.9.26.44498_x64.exe",
        "https://dldir1.qq.com/qqfile/qq/QQNT/b07cb1a5/QQ9.9.15.27597_x64.exe",
    ):
        print(f"  {probe(u):16} {u.split('/')[-1]}")

    print("\n=== 3) 新 CDN 上试探旧版本路径（推测命名规则）===")
    cands = [
        "https://qqdl.gtimg.cn/qqfile/QQNTV2/9.9.32/release/",
        "https://qqdl.gtimg.cn/qqfile/QQNTV2/9.9.26/release/",
        "https://qqdl.gtimg.cn/qqfile/QQNTV2/9.9.36/release/e8e54bbb/QQ_9.9.36_260924_x64_01.exe",
    ]
    for u in cands:
        print(f"  {probe(u):16} {u}")

    print("\n=== 4) NapCat 最新版本（确认是否已支持 9.9.36）===")
    j = json.loads(urllib.request.urlopen(urllib.request.Request(
        "https://api.github.com/repos/NapNeko/NapCatQQ/releases/latest",
        headers={"User-Agent": "Mozilla/5.0"}), timeout=30).read())
    print(f"  {j['tag_name']}  发布于 {j['published_at'][:10]}")


if __name__ == "__main__":
    main()
