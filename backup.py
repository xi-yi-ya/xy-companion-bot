"""Back up only the dedicated companion database using the global credential."""

import os
import subprocess
from datetime import datetime
from pathlib import Path

from local_secrets import database_parameters

p = database_parameters()
root = Path(__file__).resolve().parent
dest = root / "runtime" / "backups" / f"companion-{datetime.now():%Y%m%d-%H%M%S}.dump"
dest.parent.mkdir(parents=True, exist_ok=True)
env = dict(os.environ, PGPASSWORD=p["password"])
subprocess.run(
    [
        r"D:\Flie\PostgreSQL\18\bin\pg_dump.exe",
        "-h",
        p["host"],
        "-p",
        str(p["port"]),
        "-U",
        p["user"],
        "-d",
        p["dbname"],
        "-Fc",
        "-f",
        str(dest),
    ],
    env=env,
    check=True,
)
print(dest)
