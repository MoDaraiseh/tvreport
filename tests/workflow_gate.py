"""Run the workflow's inline time-window gate (step 1 of weekly-report.yml)
exactly as written in the YAML, so tests exercise the real script."""

from __future__ import annotations

import contextlib
import io
import os
from datetime import datetime
from functools import cache
from pathlib import Path
from types import CodeType

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "weekly-report.yml"


@cache
def gate_source() -> str:
    lines = WORKFLOW.read_text(encoding="utf-8").splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip() == "python3 - <<'PY'")
    end = next(i for i in range(start + 1, len(lines)) if lines[i].strip() == "PY")
    body = lines[start + 1 : end]
    indent = min(len(x) - len(x.lstrip()) for x in body if x.strip())
    return "\n".join(x[indent:] for x in body)


@cache
def _compiled() -> CodeType:
    return compile(gate_source(), "weekly-report.yml:gate", "exec")


def run_gate(now: datetime, event: str = "schedule", run_type: str = "") -> dict[str, str]:
    """Execute the gate for `now`; returns its outputs, e.g.
    {'run': 'true', 'run_type': 'monday'}."""

    env = {"GATE_NOW": now.isoformat(), "EVENT": event, "INPUT_RUN_TYPE": run_type}
    saved = {k: os.environ.get(k) for k in [*env, "GITHUB_OUTPUT"]}
    os.environ.update(env)
    os.environ.pop("GITHUB_OUTPUT", None)
    buffer = io.StringIO()
    try:
        with contextlib.redirect_stdout(buffer):
            exec(_compiled(), {"__name__": "__gate__"})
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    out: dict[str, str] = {}
    for line in buffer.getvalue().splitlines():
        if "=" in line and ":" not in line:
            key, _, value = line.partition("=")
            out[key] = value
    return out
