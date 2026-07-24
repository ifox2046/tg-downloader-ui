#!/usr/bin/env python3
"""Update Docker Hub repository short + full description from docker/DOCKERHUB.md.

Uses the same credentials as docker login (username + password/token).
Env:
  DOCKERHUB_USERNAME
  DOCKERHUB_TOKEN
Optional:
  DOCKERHUB_NAMESPACE (default: username)
  DOCKERHUB_REPOSITORY (default: tg-downloader-ui)
  DOCKERHUB_MD (default: docker/DOCKERHUB.md relative to repo root)
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def parse_dockerhub_md(text: str) -> tuple[str, str]:
    """Return (short_description, full_description) from docker/DOCKERHUB.md."""
    short = ""
    m = re.search(
        r"## Short description[^\n]*\n+\s*```[^\n]*\n(.*?)```",
        text,
        re.S | re.I,
    )
    if m:
        short = m.group(1).strip()

    full = ""
    # Prefer content between "Full description" heading and the next "---" before 中文 section
    m = re.search(
        r"## Full description[^\n]*\n+(.*?)(?:\n---\n\s*## 中文|\Z)",
        text,
        re.S | re.I,
    )
    if m:
        full = m.group(1).strip()
        # Drop a leading fenced block if present
        if full.startswith("```"):
            full = re.sub(r"^```[^\n]*\n", "", full)
            full = re.sub(r"\n```\s*$", "", full).strip()

    if not short:
        raise SystemExit("could not parse short description from DOCKERHUB.md")
    if not full:
        raise SystemExit("could not parse full description from DOCKERHUB.md")
    if len(short) > 100:
        short = short[:97] + "..."
    return short, full


def hub_login(username: str, password: str) -> str:
    body = json.dumps({"username": username, "password": password}).encode("utf-8")
    req = urllib.request.Request(
        "https://hub.docker.com/v2/users/login/",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise SystemExit(f"Docker Hub login failed: HTTP {exc.code}: {detail}") from exc
    token = str(data.get("token") or "").strip()
    if not token:
        raise SystemExit("Docker Hub login returned empty token")
    return token


def hub_patch_repo(
    token: str,
    namespace: str,
    repository: str,
    *,
    description: str,
    full_description: str,
) -> None:
    url = f"https://hub.docker.com/v2/repositories/{namespace}/{repository}/"
    body = json.dumps(
        {
            "description": description,
            "full_description": full_description,
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"JWT {token}",
        },
        method="PATCH",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read().decode("utf-8")
            code = resp.status
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise SystemExit(f"Docker Hub PATCH failed: HTTP {exc.code}: {detail}") from exc
    print(f"Updated https://hub.docker.com/r/{namespace}/{repository}/ (HTTP {code})")
    if raw:
        try:
            data = json.loads(raw)
            print("short:", (data.get("description") or "")[:100])
            print("full_len:", len(data.get("full_description") or ""))
        except json.JSONDecodeError:
            pass


def main() -> int:
    username = (os.environ.get("DOCKERHUB_USERNAME") or "").strip()
    password = (os.environ.get("DOCKERHUB_TOKEN") or "").strip()
    if not username or not password:
        print(
            "DOCKERHUB_USERNAME and DOCKERHUB_TOKEN are required",
            file=sys.stderr,
        )
        return 2

    namespace = (os.environ.get("DOCKERHUB_NAMESPACE") or username).strip()
    repository = (os.environ.get("DOCKERHUB_REPOSITORY") or "tg-downloader-ui").strip()
    md_path = Path(
        os.environ.get("DOCKERHUB_MD")
        or (repo_root() / "docker" / "DOCKERHUB.md")
    )
    text = md_path.read_text(encoding="utf-8")
    short, full = parse_dockerhub_md(text)
    print(f"Parsed short ({len(short)} chars), full ({len(full)} chars) from {md_path}")
    token = hub_login(username, password)
    hub_patch_repo(
        token,
        namespace,
        repository,
        description=short,
        full_description=full,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
