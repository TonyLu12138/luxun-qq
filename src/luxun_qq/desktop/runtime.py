"""Optional, explicitly selected local components; never downloads software."""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path


class LocalModelProcess:
    def __init__(self):
        self.process = None
        self.log_file = None

    def start(self, settings, directory: Path, cancelled, log) -> str:
        if cancelled.is_set():
            raise RuntimeError("本地模型启动已取消")
        directory.mkdir(parents=True, exist_ok=True)
        self.log_file = (directory / "local-model.log").open("ab")
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        command = [str(Path(settings.llama_exe).resolve()), "-m", str(Path(settings.gguf_path).resolve()),
                   "--host", "127.0.0.1", "--port", str(port), "--alias", "desktop-local",
                   "-c", str(settings.context_size), "-ngl", str(settings.gpu_layers)]
        try:
            self.process = subprocess.Popen(
                command, cwd=str(Path(settings.llama_exe).resolve().parent),
                stdin=subprocess.DEVNULL, stdout=self.log_file, stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            endpoint = f"http://127.0.0.1:{port}"
            log("正在加载本地模型，首次加载可能需要数分钟…")
            deadline = time.monotonic() + 300
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            while time.monotonic() < deadline:
                if cancelled.is_set():
                    raise RuntimeError("本地模型启动已取消")
                if self.process.poll() is not None:
                    raise RuntimeError("本地模型进程已退出，请查看 local-model.log。")
                try:
                    with opener.open(endpoint + "/health", timeout=1) as response:
                        if response.status == 200 and self.process.poll() is None:
                            return endpoint
                except (OSError, urllib.error.URLError):
                    pass
                cancelled.wait(0.3)
            raise RuntimeError("模型加载超过 5 分钟，请检查模型、内存和 GPU 设置。")
        except Exception:
            self.stop()
            raise

    def stop(self) -> None:
        process, self.process = self.process, None
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
        if self.log_file is not None:
            self.log_file.close()
            self.log_file = None


def import_components(source: Path, directory: Path) -> dict[str, str]:
    """Copy an optional bundled tree into a fresh writable directory.

    No executables are started here. Each import gets its own directory, so a
    different bundle can never overwrite a user's QQ profiles or local files.
    """
    if not source.is_dir():
        raise ValueError("此发行包未内置运行组件，请选择已准备好的组件目录。")
    if directory.resolve().is_relative_to(source.resolve()):
        raise ValueError("不能把组件导入到其自身的子目录，请选择独立的源目录。")
    files = list(source.rglob("*"))
    if any(p.is_symlink() or (hasattr(p, "is_junction") and p.is_junction()) for p in files):
        raise ValueError("组件目录不能包含符号链接或目录联接。")
    directory.mkdir(parents=True, exist_ok=True)
    target = Path(tempfile.mkdtemp(prefix="import-", dir=directory))
    try:
        shutil.copytree(source, target, dirs_exist_ok=True)
    except Exception:
        # Keep a failed import for inspection; don't delete possibly large trees.
        raise RuntimeError(f"组件复制失败，未完整的文件保留在：{target}") from None
    result = {}
    llama = next(target.rglob("llama-server.exe"), None)
    model = next(iter(sorted(target.rglob("*.gguf"))), None)
    launcher = target / "qq" / "launcher.exe"
    if not launcher.exists():
        launcher = target / "qq" / "launcher.bat"
    if llama:
        result["llama_exe"] = str(llama)
    if model:
        result["gguf_path"] = str(model)
    if launcher.is_file():
        result["qq_launcher"] = str(launcher)
    result["directory"] = str(target)
    return result


def launch_qq(path: str) -> None:
    launcher = Path(path).resolve()
    if not launcher.is_file() or launcher.suffix.lower() not in (".exe", ".bat", ".cmd"):
        raise ValueError("请选择已准备好的 QQ/NapCat 启动程序（exe、bat 或 cmd）。")
    if launcher.suffix.lower() in (".bat", ".cmd"):
        # cmd expands these even inside quotes. Reject rather than interpolate.
        if any(char in str(launcher) for char in '%!"\r\n'):
            raise ValueError("批处理路径不能包含 %、!、引号或换行。")
        command = f'cmd.exe /d /s /c ""{launcher}""'
    else:
        command = [str(launcher)]
    subprocess.Popen(command, cwd=str(launcher.parent), stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
