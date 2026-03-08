#!/usr/bin/env python3
"""
stop.local.py — Topos Pipeline Shutdown (Cross-Platform)
=========================================================

Features:
- Reads .lock file written by start.local.py
- Process group kill (catches adk + uvicorn workers)
- Fallback: kill by port if no .lock
- Orphan process cleanup
- Port verification after shutdown

Usage:
    python3.14 stop.local.py [--force]
"""

import json
import logging
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, cast

SIGKILL = getattr(signal, "SIGKILL", signal.SIGTERM)

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).parent.resolve()
LOCK_FILE = PROJECT_ROOT / ".adk" / ".ecosystem.lock"
DEFAULT_PORT = 8888


def is_windows() -> bool:
    return sys.platform == "win32"


def load_env() -> dict[str, str]:
    """Load .env to get configured port."""
    env_file = PROJECT_ROOT / ".env"
    parsed: dict[str, str] = {}
    if not env_file.exists():
        return parsed
    with open(env_file, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key:
                    parsed[key] = value
    return parsed


def is_port_in_use(port: int) -> bool:
    """Check if something is listening on port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1)
        try:
            s.connect(("127.0.0.1", port))
            return True
        except (ConnectionRefusedError, OSError):
            return False


def is_process_running(pid: int) -> bool:
    """Check if a process is alive."""
    try:
        os.kill(pid, 0)
        return True
    except (OSError, SystemError):
        return False


def kill_process(pid: int, force: bool = False) -> bool:
    """Kill a process and its group. Cross-platform."""
    if not is_process_running(pid):
        return True

    try:
        if is_windows():
            cmd = ["taskkill"]
            if force:
                cmd.append("/F")
            cmd.extend(["/PID", str(pid), "/T"])
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            return result.returncode == 0
        else:
            sig = SIGKILL if force else signal.SIGTERM
            # Kill entire process group (catches uvicorn workers, etc.)
            try:
                pgid = os.getpgid(pid)
                os.killpg(pgid, sig)
            except (ProcessLookupError, PermissionError):
                try:
                    os.kill(pid, sig)
                except ProcessLookupError:
                    return True

            # Wait for death
            for _ in range(10):
                if not is_process_running(pid):
                    return True
                time.sleep(0.5)

            # Force kill if still alive
            if is_process_running(pid):
                try:
                    pgid = os.getpgid(pid)
                    os.killpg(pgid, SIGKILL)
                except (ProcessLookupError, PermissionError):
                    try:
                        os.kill(pid, SIGKILL)
                    except ProcessLookupError:
                        pass
                time.sleep(1)

            return not is_process_running(pid)

    except Exception as e:
        logger.debug(f"Error killing process {pid}: {e}")
        return False


def kill_by_port(port: int) -> bool:
    """Fallback: kill process by port. Cross-platform."""
    if is_windows():
        try:
            result = subprocess.run(
                ["powershell", "-Command",
                 f"Get-NetTCPConnection -LocalPort {port} -ErrorAction SilentlyContinue "
                 f"| ForEach-Object {{ Stop-Process -Id $_.OwningProcess -Force }}"],
                capture_output=True, text=True, timeout=10
            )
            return result.returncode == 0
        except Exception:
            return False
    else:
        try:
            result = subprocess.run(
                ["lsof", "-t", f"-i:{port}"],
                capture_output=True, text=True, timeout=5
            )
            if result.stdout.strip():
                for pid_str in result.stdout.strip().split("\n"):
                    try:
                        pid = int(pid_str.strip())
                        if pid != os.getpid():
                            os.kill(pid, signal.SIGTERM)
                    except (ValueError, ProcessLookupError):
                        continue
                return True
        except FileNotFoundError:
            try:
                subprocess.run(["fuser", "-k", f"{port}/tcp"],
                               capture_output=True, timeout=5)
                return True
            except (FileNotFoundError, Exception):
                pass
    return False


def kill_orphan_processes() -> int:
    """Kill orphaned python/adk processes related to this project."""
    killed = 0
    project_path = str(PROJECT_ROOT).lower()

    if is_windows():
        try:
            result = subprocess.run(
                ["powershell", "-Command",
                 "Get-CimInstance Win32_Process | Where-Object { "
                 "$_.Name -match 'python|adk' } | "
                 "Select-Object ProcessId, Name, CommandLine | ConvertTo-Json"],
                capture_output=True, text=True, timeout=30
            )
            if result.returncode != 0 or not result.stdout.strip():
                return 0

            processes = json.loads(result.stdout)
            if isinstance(processes, dict):
                processes = [processes]

            for proc in processes:
                cmd_line = (proc.get("CommandLine") or "").lower()
                pid = proc.get("ProcessId")
                if not pid or not cmd_line:
                    continue
                if project_path in cmd_line and str(os.getpid()) != str(pid):
                    if "stop.local" not in cmd_line:
                        logger.info(f"🧹 Killing orphan: PID {pid}")
                        subprocess.run(["taskkill", "/F", "/PID", str(pid), "/T"],
                                       capture_output=True, timeout=10)
                        killed += 1
        except Exception:
            pass
    else:
        try:
            result = subprocess.run(
                ["ps", "aux"], capture_output=True, text=True, timeout=10
            )
            excluded_pids = {os.getpid(), os.getppid()}

            for line in result.stdout.split("\n"):
                line_lower = line.lower()
                if "stop.local" in line_lower:
                    continue
                if ("python" in line_lower or "adk" in line_lower) and project_path in line_lower:
                    parts = line.split()
                    if len(parts) >= 2:
                        try:
                            pid = int(parts[1])
                            if pid not in excluded_pids:
                                logger.info(f"🧹 Killing orphan: PID {pid}")
                                os.kill(pid, signal.SIGTERM)
                                for _ in range(6):
                                    time.sleep(0.5)
                                    try:
                                        os.kill(pid, 0)
                                    except OSError:
                                        break
                                else:
                                    try:
                                        os.kill(pid, SIGKILL)
                                    except ProcessLookupError:
                                        pass
                                killed += 1
                        except (ValueError, ProcessLookupError):
                            continue
        except Exception:
            pass

    if killed > 0:
        logger.info(f"✓ Killed {killed} orphan process(es)")
    return killed


class LockFile:
    """Read/delete the lock file written by start.local.py."""

    def __init__(self, path: Path):
        self.path = path

    def read(self) -> dict[str, Any] | None:
        if not self.path.exists():
            return None
        try:
            with open(self.path, encoding="utf-8") as f:
                content = f.read().strip()
                return cast(dict[str, Any], json.loads(content)) if content else None
        except (OSError, json.JSONDecodeError):
            return None

    def delete(self):
        if self.path.exists():
            try:
                self.path.unlink()
                logger.info("✓ Lock file removed")
            except OSError as e:
                logger.warning(f"⚠️  Could not remove lock file: {e}")


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Stop Topos Pipeline")
    parser.add_argument("--force", "-f", action="store_true",
                        help="Force kill (SIGKILL)")
    args = parser.parse_args()

    print("\n>>> Stopping Topos Pipeline <<<\n")

    # 1. Read lock file
    lock = LockFile(LOCK_FILE)
    lock_data = lock.read()

    # 2. Determine port
    port = DEFAULT_PORT
    if lock_data:
        port = lock_data.get("port", DEFAULT_PORT)

    # 3. Check current state
    port_busy = is_port_in_use(port)

    if not lock_data and not port_busy:
        logger.info("ℹ️  No running instance detected")
        kill_orphan_processes()
        lock.delete()
        return 0

    # 4. Show status
    logger.info("Status:")
    if lock_data:
        logger.info(f"   PID:     {lock_data.get('pid', '?')}")
        logger.info(f"   Port:    {lock_data.get('port', '?')}")
        logger.info(f"   Auth:    {lock_data.get('auth_mode', '?')}")
        logger.info(f"   Started: {lock_data.get('started_at', '?')}")
    if port_busy:
        logger.info(f"   Port {port}: in use")
    print()

    killed = 0

    # 5. Kill by PID from lock file
    if lock_data:
        pid = lock_data.get("pid")
        if pid:
            logger.info(f"🛑 Stopping ADK Web (PID: {pid})...")
            if kill_process(pid, args.force):
                logger.info("   ✓ Process stopped")
                killed += 1
            else:
                logger.warning("   ⚠️  Could not stop by PID, trying by port...")

    # 6. Fallback: kill by port
    if is_port_in_use(port):
        logger.info(f"🛑 Stopping process on port {port}...")
        if kill_by_port(port):
            logger.info("   ✓ Stopped via port")
            killed += 1

    # 7. Clean lock file
    lock.delete()

    # 8. Kill orphans
    orphans = kill_orphan_processes()
    killed += orphans

    # 9. Verify
    time.sleep(2)
    if is_port_in_use(port):
        logger.warning(f"⚠️  Port {port} still in use (may be TIME_WAIT, wait 30s)")
        return 1

    print()
    logger.info("✓ Topos Pipeline stopped cleanly")
    return 0


if __name__ == "__main__":
    sys.exit(main())
