#!/usr/bin/env python3
"""Create .env from .env.example, generating every secret left empty (idempotent).

    python3 scripts/bootstrap_env.py            # fills only what is missing; never rotates
    python3 scripts/bootstrap_env.py --print    # show which keys were generated (names only)

Generated: POSTGRES_PASSWORD, VALKEY_PASSWORD, ENGINE_CREDENTIALS_KEY (Fernet), one gateway
token per role and the admin token. Values already set in .env are kept.
"""
from __future__ import annotations

import base64
import os
import re
import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GENERATED = {
    "POSTGRES_PASSWORD": lambda: secrets.token_urlsafe(24),
    "VALKEY_PASSWORD": lambda: secrets.token_urlsafe(24),
    "ENGINE_CREDENTIALS_KEY": lambda: base64.urlsafe_b64encode(os.urandom(32)).decode(),
    **{f"ENGINE_GATEWAY_TOKEN_{r}": (lambda r=r: f"gw-{r.lower()}-{secrets.token_urlsafe(24)}")
       for r in ("ORCHESTRATOR", "EXECUTOR", "VALIDATOR", "DOCUMENTALIST", "AUTHOR", "MEDIC", "EMBEDDINGS")},
    "ENGINE_GATEWAY_ADMIN_TOKEN": lambda: secrets.token_urlsafe(32),
}
LINE = re.compile(r"^([A-Z_][A-Z0-9_]*)=(.*)$")


def main() -> int:
    env, example = ROOT / ".env", ROOT / ".env.example"
    lines = (env if env.exists() else example).read_text().splitlines()
    done = []
    for i, line in enumerate(lines):
        m = LINE.match(line)
        if m and m.group(1) in GENERATED and not m.group(2).strip():
            lines[i] = f"{m.group(1)}={GENERATED[m.group(1)]()}"
            done.append(m.group(1))
    env.write_text("\n".join(lines) + "\n")
    env.chmod(0o600)
    print(f"[env] {env.name}: generated {len(done)} value(s)" + (f": {', '.join(done)}" if "--print" in sys.argv else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
