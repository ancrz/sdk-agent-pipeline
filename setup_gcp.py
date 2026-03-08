#!/usr/bin/env python3
"""
setup_gcp.py — Guided Google Cloud + Vertex AI Setup
======================================================

Interactive script that walks through each stage of GCP setup:
1. Install gcloud CLI (if needed)
2. Authenticate with Google
3. Select/create a GCP project
4. Enable billing verification
5. Enable Vertex AI API
6. Generate Application Default Credentials (OAuth)
7. Configure .env for Vertex AI mode

Each stage pauses and explains what Google will ask the user to accept.

Usage:
    python3.14 setup_gcp.py
"""

import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.resolve()
ENV_FILE = PROJECT_ROOT / ".env"

# ANSI colors for terminal
GREEN = "\033[92m"
YELLOW = "\033[93m"
RED = "\033[91m"
CYAN = "\033[96m"
BOLD = "\033[1m"
RESET = "\033[0m"


def print_header(stage: int, total: int, title: str):
    print(f"\n{'═' * 60}")
    print(f"  {CYAN}{BOLD}Stage {stage}/{total}: {title}{RESET}")
    print(f"{'═' * 60}\n")


def print_notice(text: str):
    print(f"  {YELLOW}📋 {text}{RESET}")


def print_success(text: str):
    print(f"  {GREEN}✓ {text}{RESET}")


def print_error(text: str):
    print(f"  {RED}❌ {text}{RESET}")


def ask_continue(message: str = "Press Enter to continue (or 'q' to quit)") -> bool:
    response = input(f"\n  {BOLD}→ {message}: {RESET}").strip().lower()
    if response in ("q", "quit", "exit"):
        print("\n  Aborted by user.")
        sys.exit(0)
    return True


def run_cmd(cmd: list[str], check: bool = True, **kwargs) -> subprocess.CompletedProcess[str]:
    """Run a command and return result."""
    return subprocess.run(cmd, capture_output=True, text=True, timeout=120, **kwargs)


def find_gcloud() -> str | None:
    """Find gcloud CLI in PATH or common install locations."""
    # Check PATH first
    gcloud = shutil.which("gcloud")
    if gcloud:
        return gcloud

    # Check common install locations
    candidates = [
        Path.home() / "google-cloud-sdk" / "bin" / "gcloud",
        Path("/tmp/google-cloud-sdk/bin/gcloud"),
        Path("/usr/lib/google-cloud-sdk/bin/gcloud"),
        Path("/snap/google-cloud-cli/current/bin/gcloud"),
    ]
    if platform.system() == "Windows":
        candidates.extend([
            Path(os.environ.get("LOCALAPPDATA", "")) / "Google" / "Cloud SDK" / "google-cloud-sdk" / "bin" / "gcloud.cmd",
            Path("C:/Program Files/Google/Cloud SDK/google-cloud-sdk/bin/gcloud.cmd"),
            Path("C:/Program Files (x86)/Google/Cloud SDK/google-cloud-sdk/bin/gcloud.cmd"),
        ])

    for path in candidates:
        if path.exists():
            return str(path)

    return None


# ── Stage 1: Install gcloud CLI ─────────────────────────────────────────────


def stage_install_gcloud(total: int) -> str:
    print_header(1, total, "Install Google Cloud CLI")

    gcloud = find_gcloud()

    if gcloud:
        result = run_cmd([gcloud, "--version"])
        version_line = result.stdout.strip().split("\n")[0] if result.stdout else "unknown"
        print_success(f"gcloud CLI already installed: {version_line}")
        print(f"     Path: {gcloud}")
        return gcloud

    print("  gcloud CLI is not installed. It is required for Vertex AI.\n")

    if platform.system() == "Windows":
        print("  For Windows, download the installer:")
        print(f"  {CYAN}https://cloud.google.com/sdk/docs/install#windows{RESET}")
        print()
        print("  Or via PowerShell:")
        print(f"  {CYAN}(New-Object Net.WebClient).DownloadFile(")
        print("    \"https://dl.google.com/dl/cloudsdk/channels/rapid/GoogleCloudSDKInstaller.exe\",")
        print("    \"$env:TEMP\\GoogleCloudSDKInstaller.exe\")")
        print()
        print_notice("The installer will ask you to:")
        print("     1. Accept Google Cloud SDK License Agreement")
        print("     2. Choose install directory")
        print("     3. Optionally add gcloud to PATH")
        print()
        ask_continue("Install gcloud manually, then press Enter")
    else:
        print("  Installing via direct download (recommended)...\n")
        print_notice("This will:")
        print("     1. Download ~120MB archive to /tmp")
        print("     2. Extract to /tmp/google-cloud-sdk")
        print("     3. Add gcloud to your PATH via ~/.zshrc or ~/.bashrc")
        print()
        ask_continue()

        # Download and install
        print("  ⏳ Downloading gcloud CLI...")
        dl_result = run_cmd([
            "bash", "-c",
            "cd /tmp && "
            "curl -sSL https://dl.google.com/dl/cloudsdk/channels/rapid/downloads/"
            "google-cloud-cli-linux-x86_64.tar.gz -o gcloud.tar.gz && "
            "tar -xzf gcloud.tar.gz"
        ])

        if dl_result.returncode != 0:
            print_error("Download failed")
            print(f"     {dl_result.stderr}")
            sys.exit(1)

        print("  ⏳ Installing...")
        install_result = run_cmd([
            "/tmp/google-cloud-sdk/install.sh",
            "--quiet", "--path-update", "true"
        ])

        if install_result.returncode != 0:
            print_error("Installation failed")
            sys.exit(1)

        print_success("gcloud CLI installed")
        print("     ⚠️  You may need to restart your terminal or run:")
        print(f"     {CYAN}source ~/.zshrc{RESET}  (or ~/.bashrc)")

    # Re-check
    gcloud = find_gcloud()
    if not gcloud:
        print_error("gcloud still not found. Please install manually and re-run.")
        sys.exit(1)

    return gcloud


# ── Stage 2: Authenticate with Google ────────────────────────────────────────


def stage_authenticate(gcloud: str, total: int):
    print_header(2, total, "Authenticate with Google")

    # Check if already authenticated
    result = run_cmd([gcloud, "auth", "list", "--filter=status:ACTIVE", "--format=value(account)"])
    if result.stdout.strip():
        account = result.stdout.strip().split("\n")[0]
        print_success(f"Already authenticated as: {account}")
        response = input("\n  → Use this account? (Y/n): ").strip().lower()
        if response not in ("n", "no"):
            return

    print("  This will open your browser for Google login.\n")
    print_notice("Google will ask you to:")
    print("     1. Choose your Google account")
    print("     2. Grant 'Google Cloud SDK' access to your account")
    print("     3. You'll see permissions like:")
    print("        • View and manage your data across Google Cloud")
    print("        • View and manage your applications on Google App Engine")
    print(f"     4. Click {GREEN}'Allow'{RESET}")
    print()
    ask_continue("Press Enter to open browser for login")

    login_result = subprocess.run(
        [gcloud, "auth", "login", "--no-launch-browser"],
        timeout=300
    )

    if login_result.returncode != 0:
        print_error("Authentication failed")
        sys.exit(1)

    print_success("Authenticated with Google")


# ── Stage 3: Select/Create GCP Project ───────────────────────────────────────


def stage_select_project(gcloud: str, total: int) -> str:
    print_header(3, total, "Select Google Cloud Project")

    # List existing projects
    result = run_cmd([gcloud, "projects", "list", "--format=value(projectId,name)", "--limit=10"])

    if result.stdout.strip():
        print("  Your existing projects:\n")
        projects = []
        for line in result.stdout.strip().split("\n"):
            parts = line.split("\t")
            proj_id = parts[0].strip()
            proj_name = parts[1].strip() if len(parts) > 1 else proj_id
            projects.append(proj_id)
            print(f"     {len(projects)}. {proj_id} ({proj_name})")
        print(f"     {len(projects) + 1}. [Create new project]")
        print()

        choice = input("  → Select a project number (or enter project ID): ").strip()

        if choice.isdigit():
            idx = int(choice) - 1
            if 0 <= idx < len(projects):
                project_id = projects[idx]
            elif idx == len(projects):
                project_id = _create_project(gcloud)
            else:
                print_error("Invalid selection")
                sys.exit(1)
        else:
            project_id = choice
    else:
        print("  No existing projects found.\n")
        project_id = _create_project(gcloud)

    # Set as default project
    run_cmd([gcloud, "config", "set", "project", project_id])
    print_success(f"Project set: {project_id}")
    return project_id


def _create_project(gcloud: str) -> str:
    print("\n  Creating a new GCP project.\n")
    print_notice("Note: Project IDs must be 6-30 characters, lowercase,")
    print("     and can contain letters, numbers, and hyphens.")
    print()

    project_id = input("  → Enter a project ID (e.g., my-adk-project): ").strip()
    if not project_id:
        print_error("Project ID cannot be empty")
        sys.exit(1)

    print(f"\n  ⏳ Creating project '{project_id}'...")
    result = run_cmd([gcloud, "projects", "create", project_id])

    if result.returncode != 0:
        if "already exists" in result.stderr:
            print_notice(f"Project '{project_id}' already exists — using it")
        else:
            print_error(f"Failed to create project: {result.stderr}")
            sys.exit(1)
    else:
        print_success(f"Project created: {project_id}")

    return project_id


# ── Stage 4: Verify Billing ─────────────────────────────────────────────────


def stage_verify_billing(gcloud: str, project_id: str, total: int):
    print_header(4, total, "Verify Billing")

    result = run_cmd([
        gcloud, "billing", "projects", "describe", project_id,
        "--format=value(billingEnabled)"
    ])

    if result.stdout.strip().lower() == "true":
        print_success("Billing is enabled for this project")
        return

    print(f"  {YELLOW}⚠️  Billing is NOT enabled for project '{project_id}'.{RESET}")
    print()
    print("  Vertex AI requires a billing account (pay-per-use).")
    print("  Google Cloud offers $300 in free credits for new accounts.")
    print()
    print("  To enable billing:")
    print(f"     1. Go to: {CYAN}https://console.cloud.google.com/billing/linkedaccount?project={project_id}{RESET}")
    print("     2. Click 'Link a billing account'")
    print("     3. Select or create a billing account")
    print()
    print_notice("Google will ask you to:")
    print("     • Accept Cloud Billing terms of service")
    print("     • Provide a payment method (credit card)")
    print("     • You will NOT be charged until you exceed free credits")
    print()
    ask_continue("Enable billing in the browser, then press Enter")

    # Re-check
    result = run_cmd([
        gcloud, "billing", "projects", "describe", project_id,
        "--format=value(billingEnabled)"
    ])

    if result.stdout.strip().lower() == "true":
        print_success("Billing verified ✓")
    else:
        print(f"  {YELLOW}⚠️  Billing still not detected. Vertex AI may not work.{RESET}")
        print("     You can continue, but API calls may fail.")
        ask_continue()


# ── Stage 5: Enable Vertex AI API ───────────────────────────────────────────


def stage_enable_vertex_api(gcloud: str, project_id: str, total: int):
    print_header(5, total, "Enable Vertex AI API")

    # Check if already enabled
    result = run_cmd([
        gcloud, "services", "list", "--enabled",
        f"--project={project_id}",
        "--filter=name:aiplatform.googleapis.com",
        "--format=value(name)"
    ])

    if result.stdout.strip():
        print_success("Vertex AI API is already enabled")
        return

    print("  Enabling Vertex AI API on your project...\n")
    print_notice("This grants your project access to:")
    print("     • Gemini models (2.0-flash, 2.5-flash, 2.5-pro)")
    print("     • Custom model training and deployment")
    print("     • AI Platform prediction endpoints")
    print()

    enable_result = run_cmd([
        gcloud, "services", "enable", "aiplatform.googleapis.com",
        f"--project={project_id}"
    ])

    if enable_result.returncode != 0:
        print_error(f"Failed to enable Vertex AI API: {enable_result.stderr}")
        print("     Try manually:")
        print(f"     {CYAN}gcloud services enable aiplatform.googleapis.com --project={project_id}{RESET}")
        sys.exit(1)

    print_success("Vertex AI API enabled")


# ── Stage 6: Application Default Credentials (OAuth) ────────────────────────


def stage_oauth_credentials(gcloud: str, total: int):
    print_header(6, total, "Application Default Credentials (OAuth)")

    adc_file = Path.home() / ".config" / "gcloud" / "application_default_credentials.json"

    if adc_file.exists():
        print_success("OAuth credentials already exist")
        print(f"     File: {adc_file}")
        response = input("\n  → Re-generate credentials? (y/N): ").strip().lower()
        if response not in ("y", "yes"):
            return

    print("  This will open your browser for OAuth consent.\n")
    print_notice("Google will ask you to:")
    print("     1. Choose your Google account (same as stage 2)")
    print("     2. Grant 'Google Auth Library' access to:")
    print("        • View and manage data across Google Cloud Platform")
    print("        • View and manage Cloud SQL instances")
    print(f"     3. Click {GREEN}'Allow'{RESET}")
    print()
    print("  These credentials are saved locally and auto-refresh.")
    print(f"  Saved to: {CYAN}{adc_file}{RESET}")
    print()
    ask_continue("Press Enter to open browser for OAuth")

    oauth_result = subprocess.run(
        [gcloud, "auth", "application-default", "login"],
        timeout=300
    )

    if oauth_result.returncode != 0:
        print_error("OAuth credential generation failed")
        sys.exit(1)

    print_success("Application Default Credentials saved")


# ── Stage 7: Configure .env ─────────────────────────────────────────────────


def stage_configure_env(project_id: str, total: int):
    print_header(7, total, "Configure .env for Vertex AI")

    location = "us-central1"
    print(f"  Project:  {project_id}")
    print(f"  Location: {location}")
    print()

    loc_choice = input(f"  → Use {location}? (Y/n, or type a different region): ").strip()
    if loc_choice and loc_choice.lower() not in ("y", "yes", ""):
        location = loc_choice

    # Write .env
    env_content = (
        f"# ── Vertex AI (active — authenticated via gcloud) ──\n"
        f"GOOGLE_GENAI_USE_VERTEXAI=TRUE\n"
        f"GOOGLE_CLOUD_PROJECT={project_id}\n"
        f"GOOGLE_CLOUD_LOCATION={location}\n"
        f"\n"
        f"# ── AI Studio API key (fallback — uncomment and set VERTEXAI=FALSE) ──\n"
        f"# GOOGLE_GENAI_USE_VERTEXAI=FALSE\n"
        f"# GOOGLE_API_KEY=YOUR_API_KEY_HERE\n"
        f"\n"
        f"# ── Model Configuration ──\n"
        f"# Override the default models used by the ADK agents.\n"
        f"# Defaults are optimized for Vertex AI.\n"
        f"# ⚠️ If using AI Studio (Free Tier), use `gemini-2.0-flash-lite`\n"
        f"# for all agents to avoid 429 RESOURCE_EXHAUSTED errors (2 RPM limit on Pro).\n"
        f"# TOPOS_MODEL_ORCHESTRATOR=gemini-2.0-flash\n"
        f"# TOPOS_MODEL_ARCHON=gemini-2.5-pro\n"
        f"# TOPOS_MODEL_ONTOS=gemini-2.5-pro\n"
        f"# TOPOS_MODEL_PRAGMA=gemini-2.5-flash\n"
    )

    # Backup existing .env
    if ENV_FILE.exists():
        backup = ENV_FILE.with_suffix(".env.backup")
        shutil.copy(ENV_FILE, backup)
        print("  📋 Existing .env backed up to .env.backup")

    with open(ENV_FILE, "w", encoding="utf-8") as f:
        f.write(env_content)

    print_success(".env configured for Vertex AI")
    print(f"     Project:  {project_id}")
    print(f"     Location: {location}")


# ── Main ─────────────────────────────────────────────────────────────────────


def main():
    total_stages = 7

    print(f"\n{'═' * 60}")
    print(f"  {BOLD}Google Cloud + Vertex AI — Guided Setup{RESET}")
    print("  For Topos Pipeline (ADK Multi-Agent System)")
    print(f"{'═' * 60}")
    print()
    print("\n  This script will walk you through 7 stages:")
    print("     1. Install gcloud CLI")
    print("     2. Authenticate with Google")
    print("     3. Select/Create a GCP project")
    print("     4. Verify billing")
    print("     5. Enable Vertex AI API")
    print("     6. Generate OAuth credentials")
    print("     7. Configure .env")
    print()
    print("  At each stage, we'll explain what Google will ask you")
    print("  to accept before proceeding.")
    print()
    ask_continue()

    # Stage 1: Install gcloud
    gcloud = stage_install_gcloud(total_stages)

    # Stage 2: Authenticate
    stage_authenticate(gcloud, total_stages)

    # Stage 3: Select project
    project_id = stage_select_project(gcloud, total_stages)

    # Stage 4: Verify billing
    stage_verify_billing(gcloud, project_id, total_stages)

    # Stage 5: Enable Vertex AI
    stage_enable_vertex_api(gcloud, project_id, total_stages)

    # Stage 6: OAuth
    stage_oauth_credentials(gcloud, total_stages)

    # Stage 7: Configure .env
    stage_configure_env(project_id, total_stages)

    # Done!
    print(f"\n{'═' * 60}")
    print(f"  {GREEN}{BOLD}✅ Setup Complete!{RESET}")
    print(f"{'═' * 60}")
    print()
    print("  You can now start the Topos Pipeline:")
    print(f"     {CYAN}python3.14 start.local.py{RESET}")
    print()
    print("\n  The startup banner will show:")
    print(f"     {GREEN}🔐 Mode: Vertex AI (OAuth){RESET}")
    print(f"        Project: {project_id}")
    print("        Auth:    ✅ credentials found")
    print()
    print("  To switch back to AI Studio (free, rate-limited):")
    print("     Edit .env → set GOOGLE_GENAI_USE_VERTEXAI=FALSE")
    print("     Uncomment and set GOOGLE_API_KEY")
    print()


if __name__ == "__main__":
    main()
