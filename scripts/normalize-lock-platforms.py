"""Preserve platform dependencies after pip-compile on Linux.

Run after compilation; these versions are deliberately reviewed pins, not latest lookups.
"""

import re
from pathlib import Path

common = {
    "uvloop": ("0.23.0", 'sys_platform != "win32" and platform_python_implementation == "CPython"'),
    "tzdata": ("2026.4", 'sys_platform == "win32"'),
}
windows_dev = {
    "colorama": ("0.4.6", 'sys_platform == "win32"'),
    "pywin32": ("312", 'sys_platform == "win32"'),
}
for name in ("requirements.lock", "requirements-dev.lock"):
    path = Path(name)
    content = path.read_text()
    for package, (version, marker) in (common | (windows_dev if "dev" in name else {})).items():
        line = f"{package}=={version} ; {marker}"
        if re.search(rf"^{package}==.*$", content, flags=re.MULTILINE):
            content = re.sub(rf"^{package}==.*$", line, content, flags=re.MULTILINE)
        else:
            content += f"\n# Platform dependency retained by normalize-lock-platforms.py.\n{line}\n"
    path.write_text(content)
