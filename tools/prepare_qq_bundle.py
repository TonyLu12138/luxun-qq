"""Prepare QQ/NapCat distribution files without installing or launching them."""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import tempfile
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOWNLOADS = ROOT / "_downloads"
SOURCES = {
    "napcat": {
        "version": "4.18.28",
        "filename": "NapCat.Shell.Windows.Node.zip",
        "url": "https://github.com/NapNeko/NapCatQQ/releases/download/v4.18.28/NapCat.Shell.Windows.Node.zip",
        "size": 116682798,
        "sha256": "fb64fa3b036ad2df1a5d7c204c482694c20e4b763978c8a4968fd3474c05b4a8",
    },
    "qq": {
        "version": "9.9.30-48517",
        "filename": "QQ_9.9.30_260429_x64_01.exe",
        "url": "https://github.com/Rodert/qq-versions/releases/download/qq-windows-x64-9.9.30-20260429/QQ_9.9.30_260429_x64_01.exe",
        "size": 291623088,
        "sha256": "3d991ee6cc920a73272a72d6be934144f045852b86cc557ad45341f321565859",
    },
}


def package(downloads: dict[str, Path], output: Path, seven_zip: str | None = None):
    """Extract with 7-Zip only. Never invoke any executable in the payload."""
    extractor = seven_zip or shutil.which("7z") or r"C:\Program Files\7-Zip\7z.exe"
    if not Path(extractor).is_file():
        raise RuntimeError("7-Zip is required; pass --seven-zip with its executable path.")
    work_parent = ROOT / "build"
    work_parent.mkdir(exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="qq-pack-", dir=work_parent))
    for name, path in downloads.items():
        print(f"Extracting {name} (without running it)", flush=True)
        subprocess.run([extractor, "x", "-y", "-bso0", "-bsp0", f"-o{work / name}", str(path)], check=True)
    roots = {"QQ": work / "qq" / "Files", "NapCat": work / "napcat"}
    manifest = {
        "format": 1, "components": ["qq", "napcat"], "archive": "qq-napcat.zip",
        "qq_version": SOURCES["qq"]["version"], "napcat_version": SOURCES["napcat"]["version"],
        "qq_exe": "QQ/QQ.exe", "napcat_boot": "NapCat/napcat/NapCatWinBootMain.exe",
        "napcat_hook": "NapCat/napcat/NapCatWinBootHook.dll",
        "napcat_main": "NapCat/napcat/napcat.mjs", "napcat_package": "NapCat/napcat/qqnt.json",
        "sources": SOURCES,
    }
    for key in ("qq_exe", "napcat_boot", "napcat_hook", "napcat_main", "napcat_package"):
        prefix, relative = manifest[key].split("/", 1)
        if not (roots[prefix] / relative).is_file():
            raise RuntimeError(f"Unexpected upstream archive layout: {manifest[key]}")
    config = json.loads((roots["QQ"] / "versions" / "config.json").read_text(encoding="utf-8"))
    if config["curVersion"] != manifest["qq_version"]:
        raise RuntimeError("QQ runtime version differs from the pinned version.")
    output.mkdir(parents=True, exist_ok=True)
    archive_path = output / manifest["archive"]
    temporary = archive_path.with_suffix(".zip.part")
    size = 0
    count = 0
    print("Compressing QQ/NapCat payload…", flush=True)
    with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=5) as archive:
        for prefix, root in roots.items():
            for path in sorted(root.rglob("*")):
                if not path.is_file():
                    continue
                if path.suffix.lower() == ".gguf" or path.name.lower() == "llama-server.exe":
                    raise RuntimeError("Model weights and inference engines must not be bundled.")
                archive.write(path, f"{prefix}/{path.relative_to(root).as_posix()}")
                size += path.stat().st_size
                count += 1
                if count % 500 == 0:
                    print(f"Packed {count} files ({size / 1048576:.0f} MiB uncompressed)", flush=True)
        for filename in ("NAPCAT_LICENSE.txt", "QQ_NAPCAT_NOTICES.txt"):
            path = ROOT / "docs" / "third_party" / filename
            archive.write(path, f"licenses/{filename}")
            size += path.stat().st_size
    temporary.replace(archive_path)
    manifest.update(sha256=digest(archive_path), unpacked_bytes=size)
    (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Bundle: {output} ({archive_path.stat().st_size / 1048576:.1f} MiB)", flush=True)


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def download(source: dict, mirror: str = "") -> Path:
    DOWNLOADS.mkdir(parents=True, exist_ok=True)
    target = DOWNLOADS / source["filename"]
    if target.is_file() and target.stat().st_size == source["size"] and digest(target) == source["sha256"]:
        print(f"Verified cached {target.name}", flush=True)
        return target
    partial = target.with_suffix(target.suffix + ".part")
    print(f"Downloading {target.name} ({source['size'] / 1048576:.1f} MiB)", flush=True)
    curl = shutil.which("curl.exe") or shutil.which("curl")
    if not curl:
        raise RuntimeError("curl is required to download the pinned distribution files.")
    if mirror:
        subprocess.run([curl, "--fail", "--location", "--silent", "--show-error", "--retry", "2",
                        "--connect-timeout", "20", "--max-time", "600", "--output", str(partial),
                        mirror.rstrip("/") + "/" + source["url"]], check=True)
        if partial.stat().st_size != source["size"] or digest(partial) != source["sha256"]:
            raise RuntimeError(f"Mirror download verification failed: {partial}")
        partial.replace(target)
        print(f"SHA256 verified: {target.name}", flush=True)
        return target
    # A ranged transfer is reliable on networks that stall large release assets.
    chunks = DOWNLOADS / (source["filename"] + ".chunks")
    chunks.mkdir(exist_ok=True)
    chunk_size = 1024 * 1024
    ranges = list(range(0, source["size"], chunk_size))
    def fetch(start):
        end = min(start + chunk_size, source["size"]) - 1
        part = chunks / str(start)
        if part.is_file() and part.stat().st_size == end - start + 1:
            return part
        for attempt in range(3):
            result = subprocess.run([curl, "--fail", "--location", "--silent", "--show-error",
                "--connect-timeout", "20", "--max-time", "90", "--range", f"{start}-{end}",
                "--output", str(part), source["url"]], capture_output=True)
            if result.returncode == 0 and part.is_file() and part.stat().st_size == end - start + 1:
                return part
            time.sleep(1)
        raise RuntimeError(f"Failed downloading range {start}-{end}: {source['filename']}")
    with ThreadPoolExecutor(max_workers=12) as pool:
        for index, part in enumerate(pool.map(fetch, ranges), 1):
            if index % 16 == 0:
                print(f"{source['filename']}: {index}/{len(ranges)} chunks", flush=True)
    with partial.open("wb") as output:
        for start in ranges:
            with (chunks / str(start)).open("rb") as input_file:
                shutil.copyfileobj(input_file, output)
    if partial.stat().st_size != source["size"] or digest(partial) != source["sha256"]:
        raise RuntimeError(f"Download verification failed: {partial}")
    partial.replace(target)
    print(f"SHA256 verified: {target.name}", flush=True)
    return target


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--download-only", action="store_true")
    parser.add_argument("--mirror", default="", help="optional HTTPS release mirror; upstream SHA256 remains mandatory")
    parser.add_argument("--output", type=Path, default=ROOT / "build" / "qq-bundle")
    parser.add_argument("--seven-zip", help="7z.exe used only to extract archives")
    args = parser.parse_args()
    with ThreadPoolExecutor(max_workers=2) as pool:
        paths = list(pool.map(lambda source: download(source, args.mirror), SOURCES.values()))
    if not args.download_only:
        package(dict(zip(SOURCES, paths)), args.output.resolve(), args.seven_zip)


if __name__ == "__main__":
    main()
