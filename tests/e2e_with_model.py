# -*- coding: utf-8 -*-
"""端到端集成测试：本地模型 + 假 NapCat + 审批发送全链路。

链路：
  假 NapCat(3001) ──webhook──▶ luxun-qq 服务(8848)
      │                            │
      │                       本地模型(18080) 生成三候选
      │                            │
      │◀── send_private_msg ──── 候选投给 owner(1234567890)
      │                            │
  模拟 owner 回「1」 ──webhook──▶ 选中第 1 条
      │                            │
      │◀── send_private_msg ──── 发给 target(1987654321)
"""
import json
import subprocess
import sys
import threading
import time
import urllib.request

sys.path.insert(0, r"E:\code\luxun-qq\src")
sys.path.insert(0, r"E:\code\luxun-qq\tools")

from luxun_qq.config import Config
from luxun_qq.service import BotService, make_server

NAPCAT_PORT = 3001
SERVICE_PORT = 18999


def start_fake_napcat():
    """启动假 NapCat，记录收到的调用。"""
    proc = subprocess.Popen(
        [sys.executable, "-m", "luxun_qq.senders.echo_server", str(NAPCAT_PORT)],
        cwd=r"E:\code\luxun-qq\src",
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        encoding="utf-8", errors="replace")
    # 等它就绪
    for _ in range(40):
        time.sleep(0.5)
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{NAPCAT_PORT}/health", timeout=2)
            return proc
        except Exception:
            continue
    return proc


def post(url, payload, timeout=120):
    req = urllib.request.Request(
        url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def event(uid, text, mid):
    return {"post_type": "message", "message_type": "private",
            "self_id": 1234567890, "user_id": uid, "message_id": mid,
            "message": [{"type": "text", "data": {"text": text}}]}


def main() -> int:
    print("=" * 72)
    print("端到端集成测试：本地模型 + 假 NapCat + 审批发送")
    print("=" * 72)

    napcat = start_fake_napcat()
    print(f"假 NapCat 已启动（端口 {NAPCAT_PORT}）")

    cfg = Config.load()
    cfg.bot.target_peer = "1987654321"
    cfg.bot.owner = "1234567890"
    cfg.bot.owner_sink = "self"
    # 指向假 NapCat
    cfg.sender.adapter = "napcat"
    cfg.sender.base_url = f"http://127.0.0.1:{NAPCAT_PORT}"
    cfg.sender.access_token = ""

    svc = BotService(cfg)
    srv = make_server(cfg, svc, host="127.0.0.1", port=SERVICE_PORT, access_token="")
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    time.sleep(1)
    print(f"luxun-qq 服务已启动（端口 {SERVICE_PORT}）")
    print(f"推理后端：{cfg.llm.backend} / {cfg.llm.model} @ {cfg.llm.base_url}")

    base = f"http://127.0.0.1:{SERVICE_PORT}"
    ok = True

    # 1) 对方发来消息 -> 应生成候选并投给 owner
    print("\n[1] 1987654321 发来消息…")
    msg = "今天又加班到这个点，你睡了吗"
    t0 = time.time()
    r = post(base + "/", event(1987654321, msg, 101))
    dt = time.time() - t0
    action = r["result"].get("action")
    print(f"    处理结果：{action}（{dt:.1f}s）")
    if action != "candidates_sent":
        print("    ✗ 未生成候选")
        ok = False

    # 2) 检查假 NapCat 是否收到候选推送
    time.sleep(1)
    log = urllib.request.urlopen(f"http://127.0.0.1:{NAPCAT_PORT}/health").read()
    print(f"    假 NapCat 健康检查：{log.decode('utf-8', 'ignore')[:80]}")

    # 3) owner 回复「1」 -> 应发给目标
    print("\n[2] owner(1234567890) 回复「1」…")
    t0 = time.time()
    r2 = post(base + "/", event(1234567890, "1", 102))
    dt2 = time.time() - t0
    res2 = r2["result"]
    print(f"    处理结果：{res2.get('action')}（{dt2:.1f}s）")
    if res2.get("action") != "sent" or not res2.get("ok"):
        print("    ✗ 发送失败")
        ok = False
    else:
        print(f"    ✓ 已发送给 {res2.get('peer_id')}")

    # 4) 确认待选已清空
    print(f"\n[3] 待选队列已清空：{len(svc.pending) == 0}")

    srv.shutdown()
    srv.server_close()
    napcat.terminate()
    try:
        napcat.wait(timeout=10)
    except Exception:
        napcat.kill()

    print("\n" + "=" * 72)
    print("✓ 端到端链路打通" if ok else "✗ 端到端存在失败项")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
