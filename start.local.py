#!/usr/bin/env python3
"""
start.local.py — Topos Pipeline Launcher (Cross-Platform)
==========================================================

Features:
- Cross-platform (Linux, macOS, Windows)
- .lock file for PID tracking (shared with stop.local.py)
- Auto venv bootstrap on first run
- Port conflict detection with zombie cleanup
- Auth mode detection (AI Studio vs Vertex AI)

Usage:
    python3.14 start.local.py [--port PORT] [--mode]
"""

import json
import logging
import os
import signal
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).parent.resolve()
LOCK_FILE = PROJECT_ROOT / ".adk" / ".ecosystem.lock"
VENV_PATH = PROJECT_ROOT / ".venv"
DEFAULT_PORT = 8888


# ── Utilities ────────────────────────────────────────────────────────────────


def is_windows() -> bool:
    return sys.platform == "win32"


def load_env() -> dict[str, str]:
    """Load .env file into os.environ and return parsed values."""
    env_file = PROJECT_ROOT / ".env"
    parsed: dict[str, str] = {}

    if not env_file.exists():
        example = PROJECT_ROOT / ".env.example"
        if example.exists():
            import shutil
            shutil.copy(example, env_file)
            logger.info("✓ .env created from .env.example — edit it and re-run")
            sys.exit(1)
        else:
            logger.error("❌ No .env or .env.example found")
            sys.exit(1)

    with open(env_file, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key:
                    os.environ[key] = value
                    parsed[key] = value

    return parsed


def is_port_available(port: int) -> bool:
    """Check if nothing is listening on port (ignores TIME_WAIT)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1)
        try:
            s.connect(("127.0.0.1", port))
            return False
        except (ConnectionRefusedError, OSError):
            return True


def wait_for_port(port: int, timeout: int = 30) -> bool:
    """Wait until a service is listening on port."""
    start = time.time()
    while time.time() - start < timeout:
        if not is_port_available(port):
            return True
        time.sleep(0.5)
    return False


def kill_port_occupant(port: int) -> bool:
    """Kill whatever is occupying a port. Cross-platform."""
    if is_windows():
        return _kill_port_windows(port)

    return _kill_port_unix(port)


def _kill_port_windows(port: int) -> bool:
    """Kill port occupant on Windows via PowerShell."""
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


def _kill_port_unix(port: int) -> bool:
    """Kill port occupant on Unix via lsof/fuser."""
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


def find_python() -> str:
    """Find Python 3.10+ executable, preferring venv."""
    venv_python = VENV_PATH / ("Scripts" if is_windows() else "bin") / "python"
    if is_windows():
        venv_python = venv_python.with_suffix(".exe")
    if venv_python.exists():
        return str(venv_python)

    candidates = (
        ["py -3.14", "py -3.13", "py -3.12", "py -3", "python", "python3"]
        if is_windows()
        else ["python3.14", "python3.13", "python3.12", "python3", "python"]
    )

    for cmd in candidates:
        try:
            parts = cmd.split()
            result = subprocess.run(
                parts + ["--version"], capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0:
                version = result.stdout.strip()
                if any(f"3.{v}" in version for v in ["10", "11", "12", "13", "14"]):
                    return cmd
        except Exception:
            continue

    return "python3"


# ── Lock File ────────────────────────────────────────────────────────────────


class LockFile:
    """PID tracking lock file shared with stop.local.py."""

    def __init__(self, path: Path):
        self.path = path

    def write(self, data: dict[str, Any]):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

    def read(self) -> dict[str, Any] | None:
        if not self.path.exists():
            return None
        try:
            with open(self.path, encoding="utf-8") as f:
                content = f.read().strip()
                return json.loads(content) if content else None
        except (OSError, json.JSONDecodeError):
            return None

    def delete(self):
        if self.path.exists():
            try:
                self.path.unlink()
            except OSError:
                pass


# ── Pre-flight Checks ────────────────────────────────────────────────────────


def detect_auth_mode(env: dict[str, str], force_prompt: bool = False) -> str:
    """Detect and validate authentication mode. Returns mode label.
    If force_prompt is True, asks the user to choose and updates .env.
    """
    env_file = PROJECT_ROOT / ".env"
    
    if force_prompt:
        print("\n  🤖 Select your Intelligence Backend:")
        print("     1. 🔑 Google AI Studio (Free Tier, 2 request/min limits on Pro)")
        print("     2. 🔐 Google Vertex AI (Enterprise, requires Cloud Billing)")
        print()
        choice = input("  → Enter choice (1 or 2, default=1): ").strip()
        
        use_vertex = (choice == "2")
        
        # Update .env
        env_content = ""
        if env_file.exists():
            with open(env_file, "r", encoding="utf-8") as f:
                env_content = f.read()
        
        if "GOOGLE_GENAI_USE_VERTEXAI" in env_content:
            import re
            env_content = re.sub(
                r"^GOOGLE_GENAI_USE_VERTEXAI=.*$",
                f"GOOGLE_GENAI_USE_VERTEXAI={'TRUE' if use_vertex else 'FALSE'}",
                env_content,
                flags=re.MULTILINE
            )
        else:
            env_content += f"\nGOOGLE_GENAI_USE_VERTEXAI={'TRUE' if use_vertex else 'FALSE'}\n"
            
        with open(env_file, "w", encoding="utf-8") as f:
            f.write(env_content)
            
        print(f"  ✅ Configured for {'Vertex AI' if use_vertex else 'AI Studio'}.")
        # Reload env
        env = load_env()
        
    use_vertex = env.get("GOOGLE_GENAI_USE_VERTEXAI", "FALSE").upper() == "TRUE"

    if use_vertex:
        project = env.get("GOOGLE_CLOUD_PROJECT", "")
        location = env.get("GOOGLE_CLOUD_LOCATION", "us-central1")
        adc_file = Path.home() / ".config" / "gcloud" / "application_default_credentials.json"

        print()
        print("  🔐 Mode: Vertex AI (OAuth)")
        print(f"     Project:  {project or 'NOT SET'}")
        print(f"     Region:   {location}")

        if not project:
            logger.error("❌ GOOGLE_CLOUD_PROJECT not set in .env")
            sys.exit(1)

        if not adc_file.exists():
            logger.error("❌ No OAuth credentials found.")
            logger.error("   Run: gcloud auth application-default login")
            sys.exit(1)

        print("     Auth:     ✅ credentials found")
        return "vertex"
    else:
        api_key = env.get("GOOGLE_API_KEY", "")

        print()
        print("  🔑 Mode: AI Studio (API Key)")

        if not api_key or api_key == "YOUR_API_KEY_HERE":
            logger.error("❌ GOOGLE_API_KEY not set or is placeholder.")
            logger.error("   Get a free key here: https://aistudio.google.com/apikey")
            logger.error("   Update your .env file with GOOGLE_API_KEY=your_key")
            sys.exit(1)

        print("     Key:      ✅ configured")
        return "free"


def ensure_venv():
    """Create venv and install google-adk if needed."""
    if VENV_PATH.exists():
        return

    logger.info("⚙️  Creating virtual environment...")
    python = find_python()
    cmd = python.split() + ["-m", "venv", str(VENV_PATH)]
    result = subprocess.run(cmd, cwd=PROJECT_ROOT)
    if result.returncode != 0:
        logger.error("❌ Failed to create virtual environment")
        sys.exit(1)

    pip = str(VENV_PATH / ("Scripts" if is_windows() else "bin") / "pip")
    if is_windows():
        pip += ".exe"

    logger.info("📦 Installing google-adk...")
    subprocess.run([pip, "install", "--upgrade", "pip"], cwd=PROJECT_ROOT,
                   capture_output=True)
    result = subprocess.run([pip, "install", "google-adk"], cwd=PROJECT_ROOT)
    if result.returncode != 0:
        logger.error("❌ Failed to install google-adk")
        sys.exit(1)

    logger.info("✓ Virtual environment ready")


def handle_port_conflict(port: int):
    """Check port and try to free it if occupied."""
    if is_port_available(port):
        return

    logger.warning(f"⚠️  Port {port} is occupied. Attempting to free...")
    kill_port_occupant(port)

    for _ in range(20):
        if is_port_available(port):
            logger.info("✓ Port freed")
            return
        time.sleep(0.5)

    logger.error(f"❌ Could not free port {port}")
    logger.error(f"   Try: python3.14 start.local.py --port {port + 1}")
    sys.exit(1)


# ── Main ─────────────────────────────────────────────────────────────────────


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Start Topos Pipeline (ADK Web)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help=f"Port for ADK Web (default: {DEFAULT_PORT})")
    parser.add_argument("--force", "-f", action="store_true",
                        help="Force restart if already running")
    parser.add_argument("--mode", action="store_true",
                        help="Interactively select between AI Studio (Free) and Vertex AI")
    args = parser.parse_args()

    port = args.port

    print("\n>>> Starting Topos Pipeline <<<\n")

    # 1. Load config
    env = load_env()

    # 2. Detect auth mode (prompt if requested)
    auth_mode = detect_auth_mode(env, force_prompt=args.mode)

    # 3. Check for existing instance
    lock = LockFile(LOCK_FILE)
    existing = lock.read()

    if existing and not args.force:
        pid = existing.get("pid")
        if pid:
            try:
                os.kill(pid, 0)
                logger.info(f"⚠️  Already running (PID: {pid}, port: {existing.get('port')})")
                logger.info("   Use --force to restart, or run stop.local.py first")
                return 1
            except OSError:
                logger.info("🧹 Stale lock file found, cleaning up...")
                lock.delete()
    elif existing and args.force:
        logger.info("🔄 Force restart requested, stopping existing instance...")
        pid = existing.get("pid")
        if pid:
            try:
                if is_windows():
                    subprocess.run(["taskkill", "/F", "/PID", str(pid), "/T"],
                                   capture_output=True, timeout=10)
                else:
                    try:
                        pgid = os.getpgid(pid)
                        os.killpg(pgid, signal.SIGTERM)
                    except (ProcessLookupError, PermissionError):
                        os.kill(pid, signal.SIGTERM)
            except (ProcessLookupError, OSError):
                pass
            time.sleep(2)
        lock.delete()

    # 4. Bootstrap venv
    ensure_venv()

    # 5. Handle port conflicts
    handle_port_conflict(port)

    # 6. Build command
    adk_bin = VENV_PATH / ("Scripts" if is_windows() else "bin") / "adk"
    if is_windows():
        adk_bin = adk_bin.with_suffix(".exe")

    cmd = [str(adk_bin), "web", "--port", str(port)]

    # 7. Launch ADK Web
    print()
    logger.info(f"🚀 Launching ADK Web on http://localhost:{port}")
    logger.info(f"   Auth: {'🔐 Vertex AI' if auth_mode == 'vertex' else '🔑 AI Studio (free)'}")
    logger.info("   Agents: orchestrator → [archon, ontos, pragma]")
    logger.info("   Press Ctrl+C to stop")
    print()

    # Launch with process group for clean shutdown
    popen_kwargs: dict[str, Any] = {"cwd": PROJECT_ROOT}
    if is_windows():
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        popen_kwargs["start_new_session"] = True

    proc = subprocess.Popen(cmd, **popen_kwargs)

    # 8. Write lock file
    lock.write({
        "pid": proc.pid,
        "port": port,
        "auth_mode": auth_mode,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "python": sys.executable,
        "project_root": str(PROJECT_ROOT),
    })

    # 9. Wait for startup
    if wait_for_port(port, timeout=15):
        logger.info(f"✓ ADK Web ready at http://localhost:{port}")
    else:
        exit_code = proc.poll()
        if exit_code is not None:
            logger.error(f"❌ ADK Web crashed (exit code: {exit_code})")
            lock.delete()
            return 1
        logger.warning("⚠️  ADK Web still starting (may take a moment)...")

    # 10. Handle Ctrl+C
    def shutdown(signum, frame):
        print("\n")
        logger.info("🛑 Shutting down...")
        try:
            if is_windows():
                subprocess.run(["taskkill", "/F", "/PID", str(proc.pid), "/T"],
                               capture_output=True, timeout=10)
            else:
                try:
                    pgid = os.getpgid(proc.pid)
                    os.killpg(pgid, signal.SIGTERM)
                except (ProcessLookupError, PermissionError):
                    proc.terminate()
        except (ProcessLookupError, OSError):
            pass

        proc.wait(timeout=10)
        lock.delete()
        logger.info("✓ Stopped cleanly")
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    # 11. Wait for process to exit
    try:
        exit_code = proc.wait()
        lock.delete()
        return exit_code
    except KeyboardInterrupt:
        shutdown(None, None)
        return 0


if __name__ == "__main__":
    sys.exit(main())
