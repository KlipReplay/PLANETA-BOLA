#!/usr/bin/env python3
import platform
import subprocess

IS_WIN = platform.system() == "Windows"

if IS_WIN:
    flags = subprocess.CREATE_NO_WINDOW
    subprocess.run(["taskkill", "/F", "/IM", "cloudflared.exe"], creationflags=flags, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    # Encerra especificamente os processos pelos nomes dos scripts
    for script in ["server.py", "admin.py", "camera.py"]:
        subprocess.run(
            ["wmic", "process", "where", f"commandline like '%{script}%'", "call", "terminate"],
            creationflags=flags, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
else:
    for proc in ["server.py", "admin.py", "camera.py", "cloudflared"]:
        subprocess.run(["pkill", "-f", proc], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)