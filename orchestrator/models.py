"""Model name defaults — the single place they live.

Providers retire model versions for new keys (2026-09-25: a new Gemini key got 404
for gemini-2.5-flash and gemini-2.5-flash-lite, while gemini-3.5-flash-lite
answered), so no module hardcodes a model: roles read VANGUARD_MODEL_<ROLE>, then
VANGUARD_DEFAULT_MODEL, then DEFAULT_MODEL. Model profiles (Models tab) override all
of this per role without a deploy.
"""

from __future__ import annotations

import os
from typing import Optional

DEFAULT_MODEL = "gemini-3.5-flash-lite"


def default_model() -> str:
    return os.environ.get("VANGUARD_DEFAULT_MODEL") or DEFAULT_MODEL


def role_model(role: str) -> str:
    return os.environ.get(f"VANGUARD_MODEL_{role.upper()}") or default_model()


def fallback_model() -> Optional[str]:
    """Tried once when the primary model is retired (404), throttled (429) or overloaded (503)."""
    return os.environ.get("VANGUARD_FALLBACK_MODEL") or None
