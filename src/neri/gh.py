"""GitHub access helpers. The token is read from `gh auth token` and never printed or logged."""
from __future__ import annotations

import subprocess
from datetime import datetime, timezone

import httpx

API = "https://api.github.com"


def token() -> str:
    r = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True)
    if r.returncode != 0 or not r.stdout.strip():
        raise RuntimeError("gh auth token failed; run `gh auth login`")
    return r.stdout.strip()


def auth_ok() -> tuple[bool, str]:
    """`gh auth status` result; output is trimmed to the first line (no token is shown by gh)."""
    r = subprocess.run(["gh", "auth", "status"], capture_output=True, text=True)
    first = (r.stdout or r.stderr).strip().splitlines()
    return r.returncode == 0, (first[0] if first else "")


def rate_limits() -> dict[str, dict]:
    """Return {resource: {limit, remaining, reset_in_s}} for core/search/graphql. Free call."""
    r = httpx.get(f"{API}/rate_limit", timeout=30, headers={
        "Authorization": f"Bearer {token()}", "Accept": "application/vnd.github+json"})
    r.raise_for_status()
    now = datetime.now(timezone.utc).timestamp()
    out = {}
    for k in ("core", "search", "graphql"):
        v = r.json()["resources"][k]
        out[k] = {"limit": v["limit"], "remaining": v["remaining"], "reset_in_s": max(0, int(v["reset"] - now))}
    return out
