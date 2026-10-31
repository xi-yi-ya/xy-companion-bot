"""Small CLI fallback for the same controller used by the tray GUI."""

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from bot_controller import BotController

if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("status", "start", "stop"))
    controller = BotController(Path(__file__).resolve().parent)
    action = parser.parse_args().action
    result = getattr(controller, action)()
    print(json.dumps(asdict(result), ensure_ascii=False) if action == "status" else result)
