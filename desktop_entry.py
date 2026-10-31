"""Capture startup failures before importing Qt in a windowed executable."""

import logging
import sys
from pathlib import Path

if __name__ == "__main__":
    try:
        root = (
            Path((Path(sys.executable).parent / "companion-root.txt").read_text("utf-8").strip())
            if getattr(sys, "frozen", False)
            else Path(__file__).resolve().parent
        )
        (root / "runtime").mkdir(exist_ok=True)
        logging.basicConfig(
            filename=root / "runtime/desktop.log", encoding="utf-8", level=logging.INFO
        )
        logging.info("Desktop entry starting")
        from gui import main

        raise SystemExit(main())
    except Exception:
        logging.exception("Desktop entry failed")
        raise
