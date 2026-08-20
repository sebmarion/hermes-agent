#!/usr/bin/env python3
"""Live qualification gate for using Zeus Qwen3.8 as the *primary* researcher.

The plan requires that the primary researcher (Zeus) be able to actually reach
Zeus's OpenAI-compatible endpoint AND discover its model id before any harvest
or candidate step runs. This script is the fail-closed gate for Tasks 0/4.

Usage:
    python qualify_zeus_researcher.py --base-url http://192.168.1.92:8080/v1 \
        --api-key "$ZEUS_API_KEY" [--expected-model qwen3.8-27b]

Exit codes:
    0  /v1/models reachable and (if --expected-model given) a model id matched
    1  endpoint unreachable, auth failed, or expected model not found
    2  usage error

No credentials are printed. The API key is taken from the environment or arg
and never logged.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request


def parse_args(argv):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base-url", default=os.environ.get("ZEUS_BASE_URL", "http://192.168.1.92:8080/v1"))
    ap.add_argument("--api-key", default=os.environ.get("ZEUS_API_KEY", "local-no-auth-needed"))
    ap.add_argument("--expected-model", default=None, help="substring that must appear in a discovered model id")
    ap.add_argument("--timeout", type=float, default=10.0)
    return ap.parse_args(argv[1:] if argv and not argv[0].startswith("-") else argv)


def fetch_models(base_url: str, api_key: str, timeout: float):
    url = base_url.rstrip("/") + "/models"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {api_key}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                raise RuntimeError(f"HTTP {resp.status}")
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"HTTP {exc.code} from {url}")
    except urllib.error.URLError as exc:
        raise RuntimeError(f"connection failed: {exc.reason}")


def main(argv):
    args = parse_args(argv)
    try:
        payload = fetch_models(args.base_url, args.api_key, args.timeout)
    except (RuntimeError, json.JSONDecodeError, OSError) as exc:
        print(f"QUALIFY: FAIL — cannot reach Zeus /v1/models: {exc}", file=sys.stderr)
        return 1

    data = payload.get("data", payload if isinstance(payload, list) else [])
    ids = [m.get("id", "") for m in data if isinstance(m, dict)]
    if not ids:
        print("QUALIFY: FAIL — /v1/models returned no model ids", file=sys.stderr)
        return 1

    if args.expected_model:
        match = [i for i in ids if args.expected_model.lower() in str(i).lower()]
        if not match:
            print(
                f"QUALIFY: FAIL — expected model {args.expected_model!r} not among "
                f"{len(ids)} discovered: {[str(i) for i in ids[:10]]}",
                file=sys.stderr,
            )
            return 1
        chosen = match[0]
    else:
        chosen = ids[0]

    print(f"QUALIFY: OK — {len(ids)} model(s) discovered; using {chosen!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
