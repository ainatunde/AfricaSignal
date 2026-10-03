"""Fail CI before pytest if real backup/restore prerequisites are missing."""

import importlib
import re
import shutil
import subprocess

for tool in ("pg_dump", "pg_restore", "psql", "rclone", "openssl", "tesseract"):
    if shutil.which(tool) is None:
        raise SystemExit(f"Required executable is missing: {tool}")
for tool in ("pg_dump", "pg_restore", "psql"):
    output = subprocess.check_output([tool, "--version"], text=True)
    if not re.search(r"PostgreSQL\) 16\.", output):
        raise SystemExit(f"Expected PostgreSQL 16 client: {output.strip()}")
importlib.import_module("moto.server")
print("Required operations prerequisites are available")
