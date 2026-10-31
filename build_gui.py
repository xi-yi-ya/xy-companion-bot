"""Build the tray-only EXE; backend, models, DB and secrets stay in their places."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def main():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from desktop_art import bot_icon

    app = QApplication([])
    work = ROOT / "runtime/desktop-build"
    for generated in (work, ROOT / "runtime/desktop/CompanionBot"):
        resolved = generated.resolve()
        if ROOT / "runtime" not in resolved.parents:
            raise RuntimeError("Build output must remain within the project runtime directory")
    work.mkdir(parents=True, exist_ok=True)
    icon = work / "companion.ico"
    if not bot_icon(size=256).pixmap(256, 256).save(str(icon), "ICO"):
        raise RuntimeError("Unable to write the application icon")
    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--windowed",
        "--onedir",
        "--name",
        "CompanionBot",
        "--distpath",
        str(ROOT / "runtime/desktop"),
        "--workpath",
        str(work / "work"),
        "--specpath",
        str(work),
        "--icon",
        str(icon),
        "--exclude-module",
        "PyQt5",
        "--exclude-module",
        "PyQt6",
        "--exclude-module",
        "numpy",
        "--exclude-module",
        "PIL",
        str(ROOT / "desktop_entry.py"),
    ]
    # PostgreSQL adds ICU binaries to PATH. Their basename matches the Windows
    # ICU shim used by Qt, but the exports differ. Resolve system dependencies
    # with a build-only PATH so the freezer follows normal Python's choices.
    build_env = dict(os.environ)
    windows = Path(os.environ.get("SystemRoot", "C:/Windows"))
    build_env["PATH"] = os.pathsep.join(
        map(
            str, [Path(sys.executable).parent, Path(sys.base_prefix), windows / "System32", windows]
        )
    )
    with (work / "build.log").open("w", encoding="utf-8") as log:
        result = subprocess.run(
            command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, check=False, env=build_env
        )
    if result.returncode:
        raise RuntimeError("EXE build failed; see runtime/desktop-build/build.log")
    target = ROOT / "runtime/desktop/CompanionBot"
    icu = target / "_internal/icuuc.dll"
    if icu.exists():
        import pefile

        exports = pefile.PE(str(icu), max_symbol_exports=100000).DIRECTORY_ENTRY_EXPORT.symbols
        if b"ucnv_open" not in {entry.name for entry in exports}:
            raise RuntimeError(
                "An incompatible ICU library was collected; do not distribute this build"
            )
    (target / "companion-root.txt").write_text(str(ROOT), "utf-8")
    shutil.copy2(ROOT / "NOTICE", target / "NOTICE.txt")
    # Keep the shared-library licensing information with the local GUI build.
    for dist in (ROOT / ".venv/Lib/site-packages").glob("*dist-info"):
        if (
            dist.name.lower().startswith(("pyside6_essentials-", "shiboken6-", "psutil-"))
            and (dist / "licenses").is_dir()
        ):
            shutil.copytree(dist / "licenses", target / "licenses" / dist.name, dirs_exist_ok=True)
    exe = target / "CompanionBot.exe"
    (work / "result.json").write_text(
        json.dumps({"exe": str(exe), "size": exe.stat().st_size}, indent=2), "utf-8"
    )
    print(str(exe))
    app.quit()


if __name__ == "__main__":
    main()
