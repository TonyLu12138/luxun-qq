"""Build one Windows executable; optionally bundle prepared runtime components."""
from __future__ import annotations

import argparse
import importlib.metadata
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--components-dir", type=Path,
                        help="optional prepared llama/, models/, qq/ directory; never downloaded")
    parser.add_argument("--qq-bundle", type=Path, help="verified QQ/NapCat-only bundle produced by prepare_qq_bundle.py")
    args = parser.parse_args()
    if sys.platform != "win32":
        parser.error("Windows EXE must be built on Windows.")
    if args.qq_bundle and args.components_dir:
        parser.error("QQ-only builds cannot also embed other components.")
    work = ROOT / "build" / "desktop"
    work.mkdir(parents=True, exist_ok=True)
    # Retain dependency license text in the frozen payload and beside the EXE.
    licenses = ["Luxun QQ Desktop — third-party notices\n",
                "Python and Tcl/Tk retain their respective licenses.\n"]
    if args.qq_bundle:
        for name in ("QQ_NAPCAT_NOTICES.txt", "NAPCAT_LICENSE.txt"):
            licenses.append((ROOT / "docs" / "third_party" / name).read_text(encoding="utf-8"))
    for package in ("websocket-client", "PyYAML", "pyinstaller"):
        dist = importlib.metadata.distribution(package)
        licenses.append(f"\n--- {package} {dist.version} ---\n")
        licenses.append(dist.metadata.get("License-Expression") or dist.metadata.get("License", ""))
        for item in dist.files or []:
            if "license" in str(item).lower() and str(item).endswith((".txt", "LICENSE", "LICENSE.MIT", "LICENSE.APACHE2")):
                path = Path(dist.locate_file(item))
                if path.is_file():
                    licenses.append(path.read_text(encoding="utf-8", errors="replace"))
    for path in (Path(sys.base_prefix) / "LICENSE.txt", Path(sys.base_prefix) / "tcl" / "tcl8.6" / "license.terms",
                 Path(sys.base_prefix) / "tcl" / "tk8.6" / "license.terms"):
        if path.is_file():
            licenses.append(f"\n--- {path.name} ---\n" + path.read_text(encoding="utf-8", errors="replace"))
    notices = work / "THIRD_PARTY_NOTICES.txt"
    notices.write_text("\n".join(licenses), encoding="utf-8")
    command = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--onefile", "--windowed",
               "--name", "LuxunQQ", "--paths", str(ROOT / "src"),
               "--distpath", str(ROOT / "dist"), "--workpath", str(work / "pyinstaller"),
               "--specpath", str(work), "--exclude-module", "pytest",
               "--add-data", f"{ROOT / 'LICENSE'}{os.pathsep}.",
               "--add-data", f"{ROOT / 'docs' / 'DESKTOP.md'}{os.pathsep}.",
               "--add-data", f"{notices}{os.pathsep}."]
    if args.components_dir:
        components = args.components_dir.resolve()
        if not components.is_dir() or not any((components / name).is_dir() for name in ("llama", "models", "qq")):
            parser.error("components-dir must contain llama/, models/ or qq/.")
        if components.is_relative_to(ROOT / "src") or components == ROOT:
            parser.error("Use a dedicated components directory, not the source repository.")
        command.extend(["--add-data", f"{components}{os.pathsep}components"])
    if args.qq_bundle:
        sys.path.insert(0, str(ROOT / "src"))
        from luxun_qq.desktop.bundled_qq import inspect_bundle
        bundle = args.qq_bundle.resolve()
        print("QQ-only bundle:", inspect_bundle(bundle, verify=True), flush=True)
        # Only these two files are whitelisted; adjacent models or build caches
        # cannot accidentally become part of this distribution.
        for name in ("manifest.json", "qq-napcat.zip"):
            command.extend(["--add-data", f"{bundle / name}{os.pathsep}qq-bundle"])
    command.append(str(ROOT / "tools" / "desktop_entry.py"))
    subprocess.run(command, cwd=ROOT, check=True)
    import shutil
    shutil.copy2(notices, ROOT / "dist" / notices.name)
    shutil.copy2(ROOT / "docs" / "DESKTOP.md", ROOT / "dist" / "使用说明.md")
    shutil.copy2(ROOT / "LICENSE", ROOT / "dist" / "LICENSE")
    print("Built:", ROOT / "dist" / "LuxunQQ.exe")


if __name__ == "__main__":
    main()
