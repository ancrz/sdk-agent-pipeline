"""Step parameters: `key=value` or `key="value with spaces, commas; \\" quotes"` (M6: a PowerShell script,
an OU's DN). The one parser and renderer for the planner's checks (orchestrator/plangraph/params.py),
the deterministic executor (orchestrator/deterministic.py) and anything else that reads a box's
"Parameters:" line. No dependencies, so every image that ships deterministic.py can ship this.
"""
from __future__ import annotations

import re

PARAM_RE = re.compile(r'\b([a-z][a-z0-9_]*)=(?:"((?:[^"\\]|\\.)*)"|([^\s,;`"]+))')
_BARE = re.compile(r'[^\s,;`"]+')


def param_pairs(text: str) -> list[tuple[str, str]]:
    """Every key=value in the text, in order, with quoted values unescaped."""
    out = []
    for m in PARAM_RE.finditer(text or ""):
        quoted = m.group(2)
        # only \" and \\ are escapes: a Windows path keeps its backslashes (C:\vanguard)
        value = re.sub(r'\\(["\\])', r"\1", quoted) if quoted is not None else m.group(3)
        out.append((m.group(1), value))
    return out


def render_value(value: str) -> str:
    """The value as written back into a box's detail: bare when it can be, else quoted."""
    value = str(value)
    if value and _BARE.fullmatch(value):
        return value
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def render_pairs(pairs) -> str:
    items = pairs.items() if isinstance(pairs, dict) else pairs
    return " ".join(f"{k}={render_value(v)}" for k, v in items)
