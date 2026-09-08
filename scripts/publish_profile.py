#!/usr/bin/env python3
"""Publish the generated SVG to a GitHub profile repository."""

from __future__ import annotations

import argparse
import base64
import os
from pathlib import Path
import urllib.parse

try:
    from .generate_profile import ApiError, github_request
except ImportError:
    from generate_profile import ApiError, github_request


def request(token: str, method: str, url: str, payload: dict | None = None):
    parsed = urllib.parse.urlsplit(url)
    try:
        return github_request(
            token, method, parsed.path, payload,
            query=dict(urllib.parse.parse_qsl(parsed.query)),
        )
    except ApiError as error:
        if method == "GET" and error.status == 404:
            return None
        raise


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
    content = base64.b64encode(Path(args.svg).read_bytes()).decode()
    for attempt in range(3):
        current = request(token, "GET", f"{api_url}?ref={args.branch}")
        if current and current.get("content", "").replace("\n", "") == content:
            print("Profile SVG is already current.")
            return 0
        payload = {
            "message": "Refresh GitHub line velocity profile", "content": content,
            "branch": args.branch,
        }
        if current and current.get("sha"):
            payload["sha"] = current["sha"]
        try:
            response = request(token, "PUT", api_url, payload)
        except ApiError as error:
            if error.status != 409 or attempt == 2:
                raise
            continue
        print(f"Published {args.path} to {args.repo}: {response['commit']['html_url']}")
        return 0
    raise AssertionError("unreachable")


if __name__ == "__main__":
    raise SystemExit(main())
