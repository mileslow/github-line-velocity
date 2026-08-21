#!/usr/bin/env python3
"""Publish the generated SVG to a GitHub profile repository."""

from __future__ import annotations

import argparse
import base64
import json
import os
import urllib.error
import urllib.request


def request(token: str, method: str, url: str, payload: dict | None = None):
    body = json.dumps(payload).encode() if payload is not None else None
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "github-line-velocity",
    }
    if payload is not None:
        headers["Content-Type"] = "application/json"
    try:
        request = urllib.request.Request(
            url,
            data=body,
            headers=headers,
            method=method,
        )
        with urllib.request.urlopen(request, timeout=60) as response:
            raw = response.read()
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"GitHub API returned {error.code}: {detail[:400]}") from error
    return json.loads(raw.decode()) if raw else None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--svg", required=True)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--path", required=True)
    parser.add_argument("--branch", default="main")
    args = parser.parse_args()

    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        raise SystemExit("GH_TOKEN or GITHUB_TOKEN is required")
    api_url = f"https://api.github.com/repos/{args.repo}/contents/{args.path}"
    current = request(token, "GET", f"{api_url}?ref={args.branch}")
    content = base64.b64encode(open(args.svg, "rb").read()).decode()
    if current and current.get("content", "").replace("\n", "") == content:
        print("Profile SVG is already current.")
        return 0
    payload = {
        "message": "Refresh GitHub line velocity profile",
        "content": content,
        "branch": args.branch,
    }
    if current and current.get("sha"):
        payload["sha"] = current["sha"]
    response = request(token, "PUT", api_url, payload)
    print(f"Published {args.path} to {args.repo}: {response['commit']['html_url']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
