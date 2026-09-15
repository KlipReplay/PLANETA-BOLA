#!/usr/bin/env python3
import os
import platform
import subprocess
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
os.chdir(BASE_DIR)

IS_WIN = platform.system() == "Windows"

# Seleciona o executável correto do Python (venv ou sistema)
if IS_WIN:
    venv_py = BASE_DIR / "venv" / "Scripts" / "pythonw.exe"
    if not venv_py.exists():
        venv_py = BASE_DIR / "venv" / "Scripts" / "python.exe"
    py_bin = str(venv_py) if venv_py.exists() else sys.executable
else:
    venv_py = BASE_DIR / "venv" / "bin" / "python3"
    py_bin = str(venv_py) if venv_py.exists() else sys.executable

# Executa apenas o túnel e a interface administrativa
comandos = [
    ["cloudflared", "tunnel", "run", "seu-tunel"],
    [py_bin, str(BASE_DIR / "admin.py")]
]

kwargs = {
    "cwd": str(BASE_DIR),
    "stdout": subprocess.DEVNULL,
    "stderr": subprocess.DEVNULL,
    "stdin": subprocess.DEVNULL
}

if IS_WIN:
    kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
else:
    kwargs["start_new_session"] = True

for cmd in comandos:
    try:
        subprocess.Popen(cmd, **kwargs)
    except Exception:
        pass