"""Versioned QQ/NapCat payload. Reading/validating a bundle never launches it."""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
import stat
import subprocess
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from urllib.parse import quote


def sha256(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def safe_member(name: str) -> PurePosixPath:
    path = PurePosixPath(name.replace("\\", "/"))
    if (path.is_absolute() or not path.parts or ".." in path.parts
            or any(":" in part or part.endswith((".", " ")) for part in path.parts)):
        raise ValueError(f"组件包包含不安全路径：{name}")
    return path


def read_bundle(source: Path, *, verify: bool = False) -> dict:
    manifest = json.loads((source / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("format") != 1 or manifest.get("components") != ["qq", "napcat"]:
        raise ValueError("需要只包含 QQ 与 NapCat 的有效组件包。")
    if manifest.get("archive") != "qq-napcat.zip":
        raise ValueError("组件包压缩文件名无效。")
    for name in ("qq_exe", "napcat_boot", "napcat_hook", "napcat_main", "napcat_package"):
        safe_member(manifest[name])
    archive = source / manifest["archive"]
    if not archive.is_file():
        raise ValueError("内置 QQ/NapCat 压缩文件缺失。")
    if verify and sha256(archive) != manifest["sha256"]:
        raise ValueError("内置 QQ/NapCat 校验失败，请重新获取完整发行包。")
    return manifest


def inspect_bundle(source: Path, *, verify: bool = False) -> dict:
    manifest = read_bundle(source, verify=verify)
    with zipfile.ZipFile(source / manifest["archive"]) as archive:
        names = {str(safe_member(item.filename)) for item in archive.infolist() if not item.is_dir()}
        forbidden = [name for name in names if name.lower().endswith(".gguf")
                     or PurePosixPath(name).name.lower() == "llama-server.exe"]
        if forbidden:
            raise ValueError("QQ 专用发行包不能包含推理引擎或模型权重。")
        for key in ("qq_exe", "napcat_boot", "napcat_hook", "napcat_main", "napcat_package"):
            if manifest[key] not in names:
                raise ValueError(f"组件缺少必要文件：{manifest[key]}")
    return {"components": manifest["components"], "qq_version": manifest["qq_version"],
            "napcat_version": manifest["napcat_version"], "files": len(names),
            "archive_bytes": (source / manifest["archive"]).stat().st_size,
            "sha256": manifest["sha256"], "model_included": False, "engine_included": False}


def _runtime(directory: Path) -> dict:
    info = json.loads((directory / "desktop-runtime.json").read_text(encoding="utf-8"))
    if info.get("format") != 1:
        raise ValueError("QQ 运行目录信息无效。")
    for key in ("qq_exe", "napcat_boot", "napcat_hook", "napcat_main", "napcat_package"):
        item = safe_member(info[key])
        target = directory.joinpath(*item.parts)
        if not target.resolve().is_relative_to(directory.resolve()) or not target.is_file():
            raise ValueError(f"QQ 运行目录缺少文件：{info[key]}")
    return info


def prepare_bundle(source: Path, destination: Path, progress=lambda _: None) -> dict:
    """Extract on the target user's explicit action; reuse unchanged components.

    A version/hash-specific directory preserves login data across app restarts.
    Neither QQ nor NapCat is executed by this function.
    """
    manifest = read_bundle(source)
    suffix = manifest["sha256"]
    if len(suffix) != 64 or any(c not in "0123456789abcdef" for c in suffix):
        raise ValueError("组件包 SHA256 无效。")
    destination.mkdir(parents=True, exist_ok=True)
    target = destination / ("qq-napcat-" + suffix[:16])
    if target.exists():
        info = _runtime(target)
        if info.get("archive_sha256") != suffix:
            raise ValueError("已有 QQ 运行目录校验信息不匹配。")
        return runtime_settings(target)
    progress("正在校验内置 QQ/NapCat…")
    inspect_bundle(source, verify=True)
    if shutil.disk_usage(destination).free < int(manifest.get("unpacked_bytes", 0)) + 256 * 1024 * 1024:
        raise ValueError("用户数据目录磁盘空间不足，无法释放 QQ/NapCat。")
    staging = Path(tempfile.mkdtemp(prefix="qq-prepare-", dir=destination))
    with zipfile.ZipFile(source / manifest["archive"]) as archive:
        entries = archive.infolist()
        # Validate the entire archive before writing a single extracted member.
        for entry in entries:
            safe_member(entry.filename)
            if stat.S_ISLNK(entry.external_attr >> 16):
                raise ValueError("组件包不能包含符号链接。")
        for index, entry in enumerate(entries):
            out = staging.joinpath(*safe_member(entry.filename).parts)
            if entry.is_dir():
                out.mkdir(parents=True, exist_ok=True)
            else:
                out.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(entry) as src, out.open("wb") as dst:
                    shutil.copyfileobj(src, dst, length=1024 * 1024)
            if index % 50 == 0:
                progress(f"正在释放 QQ/NapCat：{index + 1}/{len(entries)}")
    info = {**manifest, "archive_sha256": suffix, "ws_port": 3001, "webui_port": 6099,
            "ws_token": secrets.token_urlsafe(32), "webui_token": secrets.token_urlsafe(32)}
    napcat_dir = staging / Path(manifest["napcat_main"]).parent
    config = napcat_dir / "config"
    config.mkdir(parents=True, exist_ok=True)
    webui = {"host": "127.0.0.1", "port": info["webui_port"], "prefix": "",
             "token": info["webui_token"], "loginRate": 3, "autoLoginAccount": ""}
    onebot = {"network": {"httpServers": [], "httpClients": [], "websocketClients": [],
              "websocketServers": [{"name": "luxun-desktop", "enable": True,
               "host": "127.0.0.1", "port": info["ws_port"], "messagePostFormat": "array",
               "reportSelfMessage": False, "token": info["ws_token"],
               "enableForcePushEvent": True, "debug": False, "heartInterval": 30000}]},
               "musicSignUrl": "", "enableLocalFile2Url": False, "parseMultMsg": False}
    # NapCat uses this template for a QQ account's first login.
    for filename, value in (("webui.json", webui), ("onebot11.json", onebot)):
        (config / filename).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    (staging / "desktop-runtime.json").write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
    _runtime(staging)
    staging.rename(target)
    progress("内置 QQ/NapCat 已就绪。")
    return runtime_settings(target)


def runtime_settings(directory: Path) -> dict:
    info = _runtime(directory)
    return {"qq_bundle_dir": str(directory), "ws_url": f"ws://127.0.0.1:{info['ws_port']}",
            "ws_token": info["ws_token"], "directory": str(directory)}


def login_url(directory: Path) -> str:
    info = _runtime(directory)
    return f"http://127.0.0.1:{info['webui_port']}/webui/?token=" + quote(info["webui_token"], safe="")


def launch_bundle(directory: Path):
    """Only called after a user clicks the launch button; no installer is run."""
    info = _runtime(directory)
    napcat = (directory / info["napcat_main"]).parent
    main_script = (directory / info["napcat_main"]).resolve()
    load_script = napcat / "loadNapCat.js"
    load_script.write_text("(async () => { await import(" + json.dumps(main_script.as_uri()) + "); })();\n",
                           encoding="utf-8")
    environment = os.environ.copy()
    environment.update({
        "NAPCAT_WORKDIR": str(napcat.resolve()),
        "NAPCAT_PATCH_PACKAGE": str((directory / info["napcat_package"]).resolve()),
        "NAPCAT_LOAD_PATH": str(load_script.resolve()),
        "NAPCAT_INJECT_PATH": str((directory / info["napcat_hook"]).resolve()),
        "NAPCAT_LAUNCHER_PATH": str((directory / info["napcat_boot"]).resolve()),
        "NAPCAT_MAIN_PATH": str(main_script),
    })
    log_path = directory / "napcat-launch.log"
    with log_path.open("ab") as log:
        return subprocess.Popen([environment["NAPCAT_LAUNCHER_PATH"],
                                 str((directory / info["qq_exe"]).resolve()),
                                 environment["NAPCAT_INJECT_PATH"]],
                                cwd=str(napcat), env=environment, stdin=subprocess.DEVNULL,
                                stdout=log, stderr=subprocess.STDOUT,
                                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
