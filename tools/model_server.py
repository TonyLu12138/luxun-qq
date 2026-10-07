# -*- coding: utf-8 -*-
"""启动/停止本地模型服务（LM Studio 自带的 llama.cpp CUDA 后端）。

为什么用这个而不是 LM Studio 图形界面：
    本机沙箱只允许写入 E:\\code，LM Studio GUI 安装器需要写 Program Files 与
    %LOCALAPPDATA%，会被拦住。而 llama-server 就是 LM Studio 包里自带的
    同一个推理后端，直接跑它能得到完全相同的推理能力与 GPU 加速，
    同时参数（显存、上下文）完全可控、可脚本化。

用法：
    python tools/model_server.py start            # 启动（16K 上下文，GPU 全量 offload）
    python tools/model_server.py start --ctx 8192
    python tools/model_server.py status           # 查看状态与显存占用
    python tools/model_server.py stop             # 停止
    python tools/model_server.py test             # 发一条推理请求验证
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "_runtime"

#: llama-server 后端的候选位置，按顺序探测。
#:
#: 为什么要探测而不是写死：仓库里**不含**这些二进制（单个几百 MB，
#: 远超 GitHub 的 100MB 单文件上限），使用者需要自己准备。可能来自：
#:   1. LM Studio 自带的 CUDA 后端（本项目开发时用的就是这个）
#:   2. 官方 llama.cpp 的 Windows CUDA 发行版（解压到 _runtime/llama.cpp）
#:   3. 任何手动放置的位置
#: 只要目录里有 llama-server.exe 就被采用，避免"换个来源就跑不起来"。
_BACKEND_CANDIDATES = [
    RUNTIME / "lmstudio" / ".bundle" / "bin" / "extensions" / "backends"
    / "llama.cpp-win-x86_64-nvidia-cuda-avx2-2.41.0",
    RUNTIME / "llama.cpp",
    RUNTIME / "llama.cpp" / "build" / "bin",
]


def _find_backend() -> Path | None:
    """返回第一个含 llama-server.exe 的目录。"""
    env = os.environ.get("LUXUN_LLAMA_BACKEND")
    if env and (Path(env) / "llama-server.exe").exists():
        return Path(env)
    for d in _BACKEND_CANDIDATES:
        if (d / "llama-server.exe").exists():
            return d
    # 兜底：_runtime 下任意深度找一次（用户可能解压到别处）
    if RUNTIME.exists():
        for hit in RUNTIME.rglob("llama-server.exe"):
            return hit.parent
    return None


BACKEND = _find_backend() or _BACKEND_CANDIDATES[0]

VENDOR = (RUNTIME / "lmstudio" / ".bundle" / "bin" / "extensions"
          / "backends" / "vendor" / "win-llama-cuda-vendor-v2")
MODEL = (ROOT / "_models" / "qwen2.5-7b-instruct-q4_k_m"
         / "qwen2.5-7b-instruct-q4_k_m-00001-of-00002.gguf")
LOG = RUNTIME / "model_server.log"
PID_FILE = RUNTIME / "model_server.pid"
#: supervisor 自身的 PID。stop 时必须先杀它，否则它会立刻把 llama-server 拉起来。
SUP_PID_FILE = RUNTIME / "model_server.supervisor.pid"
ALIAS = "qwen2.5-7b-instruct"

# 保持 Job Object 句柄存活：句柄一旦关闭，job 就销毁并终止里面所有进程。
# 这正是我们要的语义——父进程结束（无论怎么结束）就带走 llama-server。
_JOB_HANDLE = None


def _bind_child_to_job(proc: subprocess.Popen) -> bool:
    """把子进程放进「父死子亡」的 Job Object（仅 Windows）。

    为什么必须这么做：
        llama-server 常驻吃 ~8GB 显存。只靠 ``except KeyboardInterrupt``
        是不够的——用户点终端窗口右上角关闭时，进程收到的是
        ``CTRL_CLOSE_EVENT``，Windows 只给几秒且不保证 Python 能跑完清理代码，
        于是 llama-server 变成孤儿进程继续占着显存。

        Job Object 由内核负责：只要持有句柄的进程消失，job 关闭，
        里面所有进程被一并终止。Ctrl+C、关窗口、taskkill、崩溃都覆盖。

    返回 True 表示已成功挂上（失败也不影响启动，只是退回旧行为）。
    """
    global _JOB_HANDLE
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [(n, ctypes.c_ulonglong) for n in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class BASIC_LIMIT(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", wintypes.LARGE_INTEGER),
                ("PerJobUserTimeLimit", wintypes.LARGE_INTEGER),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD)]

        class EXT_LIMIT(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", BASIC_LIMIT),
                        ("IoInfo", IO_COUNTERS),
                        ("ProcessMemoryLimit", ctypes.c_size_t),
                        ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t),
                        ("PeakJobMemoryUsed", ctypes.c_size_t)]

        JobObjectExtendedLimitInformation = 9
        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000

        job = k32.CreateJobObjectW(None, None)
        if not job:
            return False
        info = EXT_LIMIT()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not k32.SetInformationJobObject(
                job, JobObjectExtendedLimitInformation,
                ctypes.byref(info), ctypes.sizeof(info)):
            k32.CloseHandle(job)
            return False

        PROCESS_SET_QUOTA, PROCESS_TERMINATE = 0x0100, 0x0001
        h = k32.OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, proc.pid)
        if not h:
            k32.CloseHandle(job)
            return False
        if not k32.AssignProcessToJobObject(job, h):
            k32.CloseHandle(h)
            k32.CloseHandle(job)
            return False
        k32.CloseHandle(h)
        _JOB_HANDLE = job          # 故意不关闭：句柄活着，约束才活着
        return True
    except Exception:
        return False


def _kill_child(proc: "subprocess.Popen | None") -> None:
    """尽力终止子进程（先温柔后强硬）。"""
    if proc is None or proc.poll() is not None:
        return
    try:
        proc.terminate()
        try:
            proc.wait(timeout=8)
            return
        except subprocess.TimeoutExpired:
            pass
    except Exception:
        pass
    try:
        proc.kill()
        proc.wait(timeout=5)
    except Exception:
        pass
    # 兜底：按镜像名清一遍（可能还有残留的其他实例）
    try:
        subprocess.run(["taskkill", "/IM", "llama-server.exe", "/F"],
                       capture_output=True)
    except Exception:
        pass



def _env() -> dict:
    env = dict(os.environ)
    env["PATH"] = f"{BACKEND};{VENDOR};" + env.get("PATH", "")
    # 固定 CUDA 的 PTX/kernel JIT 缓存目录，避免每次重启都重新编译
    # （首次编译约 5 分钟，命中缓存后是秒级）
    cache = RUNTIME / "cuda_cache"
    cache.mkdir(parents=True, exist_ok=True)
    env["CUDA_CACHE_PATH"] = str(cache)
    env["CUDA_CACHE_MAXSIZE"] = str(2 * 1024 * 1024 * 1024)   # 2GB
    return env


def _build_args(ctx: int, port: int, gpu_layers: int, bind: str) -> list[str]:
    return [str(BACKEND / "llama-server.exe"),
            "-m", str(MODEL),
            "--host", bind, "--port", str(port),
            "-ngl", str(gpu_layers),          # GPU 层数：99 = 全部
            "-c", str(ctx),
            "-fa", "on",                      # flash attention，省显存
            "--jinja",                        # 用模型自带 chat 模板
            "--alias", ALIAS,
            "--no-warmup"]


def _wait_ready(proc, port: int, timeout_s: int, label: str) -> bool:
    for i in range(max(1, timeout_s // 3)):
        time.sleep(3)
        if proc.poll() is not None:
            print(f"[{label}] 进程退出 code={proc.returncode}，日志尾部：", flush=True)
            for f in (LOG, Path(str(LOG) + ".err")):
                if f.exists():
                    print(f"--- {f.name} ---", flush=True)
                    print("\n".join(f.read_text(encoding="utf-8",
                                               errors="replace").splitlines()[-25:]),
                          flush=True)
            return False
        if status(port, quiet=True):
            print(f"[{label}] ✔ 就绪（约 {(i + 1) * 3}s）", flush=True)
            return True
    print(f"[{label}] 等待超时", flush=True)
    return False


def supervise(ctx: int, port: int, gpu_layers: int, bind: str) -> int:
    """常驻监管：保持模型服务一直可用，退出时确保释放显存。

    为什么需要它：DSH 会在后台任务结束时清理该任务启动的整棵进程树，
    因此不能"启动后就退出"。本进程长期驻留，作为父进程守护 llama-server，
    意外退出时自动重启。

    清理保证（这是显存不释放问题的修复点）：
      * child 挂进 Job Object —— 父进程以任何方式死亡都会带走它；
      * Ctrl+C / SIGTERM 走显式终止；
      * atexit 兜底；
      * 退出前再按镜像名扫一遍残留的 llama-server。
    """
    import atexit
    import signal

    if not BACKEND.exists() or not MODEL.exists():
        print("缺少后端或模型文件，无法启动。")
        print(f"  backend = {BACKEND}  （存在：{BACKEND.exists()}）")
        print(f"  model   = {MODEL}  （存在：{MODEL.exists()}）")
        print("  后端获取方式见 `python tools/model_server.py start` 的提示；")
        print("  模型可运行 `python tools/setup.py --model` 自动下载。")
        return 1

    RUNTIME.mkdir(parents=True, exist_ok=True)
    print(f"监管模式启动：端口 {port}，上下文 {ctx}，GPU 层 {gpu_layers}", flush=True)
    print("（Ctrl+C 或关闭本窗口都会自动释放显存）", flush=True)

    state: dict = {"proc": None, "stopping": False}

    def _cleanup(*_a) -> None:
        """幂等清理：终止子进程并尽量清掉残留。"""
        if state["stopping"]:
            return
        state["stopping"] = True
        print("\n[supervisor] 正在停止模型服务并释放显存…", flush=True)
        _kill_child(state.get("proc"))
        # 再扫一遍：可能有更早遗留的孤儿实例
        try:
            out = subprocess.run(["taskkill", "/IM", "llama-server.exe", "/F"],
                                 capture_output=True, text=True)
            if out.returncode == 0:
                print("[supervisor] 已清理残留的 llama-server 进程", flush=True)
        except Exception:
            pass
        try:
            PID_FILE.unlink(missing_ok=True)
        except Exception:
            pass
        try:
            SUP_PID_FILE.unlink(missing_ok=True)
        except Exception:
            pass
        _print_vram()

    atexit.register(_cleanup)
    for sig in ("SIGINT", "SIGTERM", "SIGBREAK"):
        s = getattr(signal, sig, None)
        if s is None:
            continue
        try:
            signal.signal(s, lambda *_a: (_cleanup(), sys.exit(0)))
        except Exception:
            pass

    try:
        SUP_PID_FILE.write_text(str(os.getpid()), encoding="ascii")
    except Exception:
        pass

    log = open(LOG, "w", encoding="utf-8", errors="replace")
    errlog = open(str(LOG) + ".err", "w", encoding="utf-8", errors="replace")
    try:
        while True:
            if status(port, quiet=True):
                print("[supervisor] 服务已在运行，30s 后复查", flush=True)
                time.sleep(30)
                continue

            print("[supervisor] 启动 llama-server…", flush=True)
            proc = subprocess.Popen(_build_args(ctx, port, gpu_layers, bind),
                                    stdout=log, stderr=errlog, env=_env(),
                                    cwd=str(BACKEND))
            state["proc"] = proc
            bound = _bind_child_to_job(proc)
            PID_FILE.write_text(str(proc.pid), encoding="ascii")
            print(f"[supervisor] PID={proc.pid}，等待就绪…"
                  f"{'（已挂 Job Object，父进程退出即释放显存）' if bound else ''}",
                  flush=True)

            if _wait_ready(proc, port, 600, "supervisor"):
                _print_vram()
                # 就绪后持续盯着，退出就重来
                while proc.poll() is None:
                    time.sleep(15)
                print(f"[supervisor] llama-server 退出 code={proc.returncode}，"
                      f"5s 后重启", flush=True)
            else:
                print("[supervisor] 启动失败，30s 后重试", flush=True)
            state["proc"] = None
            time.sleep(5)
    except KeyboardInterrupt:
        _cleanup()
        return 0
    finally:
        try:
            log.close()
        except Exception:
            pass
        try:
            errlog.close()
        except Exception:
            pass
        _cleanup()


def start(ctx: int, port: int, gpu_layers: int, bind: str) -> int:
    if not BACKEND.exists():
        print(f"缺少推理后端（llama-server.exe）：{BACKEND}")
        print("本项目仓库里**不含**这些二进制（单个几百 MB，超出 GitHub 限制）。")
        print("获取方式二选一：")
        print("  A) 装 LM Studio 后，把它的 CUDA llama.cpp 后端目录放到：")
        print(f"     {_BACKEND_CANDIDATES[0]}")
        print("  B) 下载官方 llama.cpp 的 Windows CUDA 发行版，解压到：")
        print(f"     {_BACKEND_CANDIDATES[1]}   （需含 llama-server.exe 与 CUDA DLL）")
        print("  也可以设环境变量 LUXUN_LLAMA_BACKEND 指向任意含 llama-server.exe 的目录。")
        print("  详见 tools/setup.py 的说明。")
        return 1
    if not MODEL.exists():
        print(f"缺少模型文件：{MODEL}")
        print("  运行 `python tools/setup.py --model` 可自动从 ModelScope 下载。")
        return 1

    if status(port, quiet=True):
        print(f"端口 {port} 上已有服务在运行，无需重复启动。")
        return 0

    RUNTIME.mkdir(parents=True, exist_ok=True)
    print("启动本地模型服务…")
    print("  后端  : llama.cpp CUDA (LM Studio 2.41.0)")
    print(f"  模型  : {MODEL.name}")
    print(f"  上下文: {ctx}   GPU 层: {gpu_layers}   端口: {port}")
    print(f"  日志  : {LOG}")

    out = open(LOG, "w", encoding="utf-8", errors="replace")
    err = open(str(LOG) + ".err", "w", encoding="utf-8", errors="replace")
    proc = subprocess.Popen(_build_args(ctx, port, gpu_layers, bind),
                            stdout=out, stderr=err, env=_env(), cwd=str(BACKEND))
    PID_FILE.write_text(str(proc.pid), encoding="ascii")
    print(f"  PID   : {proc.pid}")
    print("等待就绪（首次运行需 PTX JIT 编译，可能 1-5 分钟）…", flush=True)

    if not _wait_ready(proc, port, 600, "start"):
        return 1
    _print_vram()
    print(f"\nOpenAI 兼容接口： http://{bind}:{port}/v1/chat/completions")
    print(f"模型名：{ALIAS}")
    print("\n注意：若本进程被结束，服务也会随之停止。")
    print("建议用 `python tools/model_server.py supervise` 常驻守护。")
    return 0


def status(port: int, quiet: bool = False) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=3) as r:
            ok = r.status == 200
    except Exception:
        ok = False
    if not quiet:
        print(f"服务状态：{'运行中' if ok else '未运行'}（端口 {port}）")
        if ok:
            try:
                with urllib.request.urlopen(
                        f"http://127.0.0.1:{port}/v1/models", timeout=5) as r:
                    data = json.loads(r.read())
                    print("  已加载模型：", [m.get("id") for m in data.get("data", [])])
            except Exception:
                pass
        _print_vram()
    return ok


def _print_vram() -> None:
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used,memory.total",
             "--format=csv,noheader"], capture_output=True, text=True, timeout=15)
        if out.stdout.strip():
            print(f"  显存占用：{out.stdout.strip()}")
    except Exception:
        pass


def _find_supervisors() -> list[int]:
    """找出所有正在跑 ``model_server.py supervise`` 的 python 进程。

    用途：清理那些**没留下 PID 文件**的旧实例（例如升级脚本之前启动的）。
    这些进程本身只占十几 MB，但它会在几十秒内把 llama-server 重新拉起来，
    于是显存又被占住——所以必须一并清掉。
    """
    script = (
        "Get-CimInstance Win32_Process "
        "-Filter \"Name='python.exe' or Name='pythonw.exe'\" | "
        "Where-Object { $_.CommandLine -like '*model_server.py*' -and "
        "$_.CommandLine -like '*supervise*' } | "
        "ForEach-Object { $_.ProcessId }"
    )
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=30)
    except Exception:
        return []
    pids: list[int] = []
    for line in (out.stdout or "").splitlines():
        line = line.strip()
        if line.isdigit():
            pids.append(int(line))
    return [p for p in pids if p != os.getpid()]


def stop() -> int:
    """停止模型服务并释放显存。

    顺序很重要：**先杀 supervisor，再杀 llama-server**。
    反过来的话，还活着的 supervisor 会在几秒内把 llama-server 重新拉起来，
    显存就又占上了。
    """
    killed = 0

    # 1) 先停 supervisor（否则它会重启模型服务）
    for f, label in ((SUP_PID_FILE, "supervisor"), (PID_FILE, "llama-server")):
        if not f.exists():
            continue
        try:
            pid = int(f.read_text(encoding="ascii").strip())
            out = subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                                 capture_output=True, text=True)
            if out.returncode == 0:
                killed += 1
                print(f"  已停止 {label}（PID {pid}）", flush=True)
        except Exception:
            pass
        f.unlink(missing_ok=True)

    # 2) 兜底：找出没登记 PID 的旧 supervisor 一并清掉
    for pid in _find_supervisors():
        try:
            out = subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                                 capture_output=True, text=True)
            if out.returncode == 0:
                killed += 1
                print(f"  已停止未登记的 supervisor（PID {pid}）", flush=True)
        except Exception:
            pass

    # 3) 兜底：按镜像名清掉所有残留（含更早遗留的孤儿进程）
    out = subprocess.run(["taskkill", "/IM", "llama-server.exe", "/F"],
                         capture_output=True, text=True)
    if out.returncode == 0:
        killed += 1
        print("  已清理残留的 llama-server", flush=True)

    print(f"已停止模型服务（{killed} 个动作）")
    _print_vram()
    return 0


def test(port: int) -> int:
    """发一条实际推理请求，验证模型可用与速度。"""
    if not status(port, quiet=True):
        print(f"端口 {port} 上没有服务，请先 start。")
        return 1
    sys_p = (
        "你是鲁迅式锐评腔的聊天分身。只输出一个 JSON 对象，结构："
        '{"step1_read":{"literal":"","intent":""},'
        '"step2_emotion":{"emotion":"","intensity":0},'
        '"step3_strategy":{"strategy":"","candidates":[{"text":""},{"text":""},{"text":""}]}}'
    )
    body = json.dumps({
        "model": ALIAS,
        "messages": [{"role": "system", "content": sys_p},
                     {"role": "user", "content": "对方说：今天又加班到这个点，你睡了吗"}],
        "temperature": 0.9, "max_tokens": 700,
    }, ensure_ascii=False).encode("utf-8")

    req = urllib.request.Request(f"http://127.0.0.1:{port}/v1/chat/completions",
                                 data=body,
                                 headers={"Content-Type": "application/json"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=300) as r:
        data = json.loads(r.read())
    dt = time.time() - t0
    text = data["choices"][0]["message"]["content"]
    print(f"耗时 {dt:.1f}s，usage={data.get('usage')}")
    print(text)
    try:
        obj = json.loads(text)
        n = len(obj["step3_strategy"]["candidates"])
        print(f"\n✔ 返回合法 JSON，候选数={n}")
    except Exception as e:
        print(f"\n⚠ 返回不是合法 JSON：{e}")
        return 1
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="本地模型服务管理")
    ap.add_argument("action",
                    choices=["start", "stop", "status", "test", "supervise"])
    ap.add_argument("--ctx", type=int, default=16384)
    ap.add_argument("--port", type=int, default=18080)
    ap.add_argument("--gpu-layers", type=int, default=99)
    ap.add_argument("--bind", default="127.0.0.1")
    a = ap.parse_args()
    if a.action == "start":
        return start(a.ctx, a.port, a.gpu_layers, a.bind)
    if a.action == "supervise":
        return supervise(a.ctx, a.port, a.gpu_layers, a.bind)
    if a.action == "stop":
        return stop()
    if a.action == "status":
        return 0 if status(a.port) else 1
    return test(a.port)


if __name__ == "__main__":
    raise SystemExit(main())
