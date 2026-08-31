#!/usr/bin/env python3
"""Harvest Seb's X bookmarks (read-only) → actionable optimization ideas.

The improve loop takes ideas from X bookmarks, but most saved posts are just
reading material and would waste a Zeus call. So this module applies a CHEAP
keyword pre-filter BEFORE any LLM spend; only posts that look Hermes-actionable
(a repo/tool/skill/hook/plugin/fix reference) proceed to the fixer pipeline.
Everything else buckets to a digest sidecar — read later, never sent to the
proposer.

Core is PURE (`filter_actionable`, `is_actionable`, `build_sidecar`) so tests
run offline on fixture dicts shaped like xurl bookmark records:
    {id, full_text, url}

Safety: credential-shaped substrings are redacted before a bookmark can be
written to disk. The CLI first tries `xurl bookmarks` as a read subprocess,
then uses a bounded read-only browser session if xurl is unavailable; neither
path reads ~/.xurl credentials or writes to X.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

from bounded_subprocess import OutputLimitExceeded, run_text_bounded

# Cheap actionability keywords: present => likely Hermes-actionable.
# Deliberately narrow + lowercase so noise rarely passes the gate.
_ACTION_KEYWORDS = (
    "hermes", "agent", "skill", "cli", "tool", "repo", "github",
    "llm", "prompt", "automation", "workflow", "plugin", "hook",
    "autonomous", "model", "api", "pipeline", "terminal", "n8n",
)

# Credential patterns (subset shared with validator).
CRED_PATTERNS = [
    (re.compile(r"(?i)(api[_-]?key|apikey)\s*[:=]?\s*['\"]?[A-Za-z0-9_\-]{16,}"), "api key"),
    (re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"), "sk secret"),
    (re.compile(r"\bghp_[A-Za-z0-9]{20,}\b"), "github token"),
    (re.compile(r"eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"), "JWT"),
]


def _scrub(text: str) -> str:
    out = text
    for pat, label in CRED_PATTERNS:
        out = pat.sub(f"[redacted:{label}]", out)
    return out


def is_actionable(bookmark: dict) -> bool:
    """Cheap keyword gate: does this post look Hermes-actionable?

    Runs on the raw text only, no model needed. Conservative on purpose —
    false negatives go to the digest (harmless); we want few false positives
    (which would burn a Zeus propose call on noise)."""
    if not isinstance(bookmark, dict):
        return False
    text = (bookmark.get("full_text") or "").lower()
    url = (bookmark.get("url") or "").lower()
    haystack = f"{text} {url}"
    return any(kw in haystack for kw in _ACTION_KEYWORDS)


def filter_actionable(bookmarks: list[dict]) -> list[dict]:
    seen = set()

    def keep(b):
        bid = b.get("id")
        if bid in seen:
            return False
        seen.add(bid)
        return is_actionable(b)

    return [b for b in bookmarks if keep(b)]


def partition(actionable, bookmarks):
    """Split into (actionable_kept, digest). Kept entries are deduped;
    digest holds everything (both noise and duplicates-not-yet-processed)."""
    kept, digest, seen = [], [], set()
    for b in bookmarks:
        bid = b.get("id")
        digest.append({"id": bid, "full_text": _scrub(str(b.get("full_text", "")))[:400],
                       "url": _scrub(str(b.get("url", "")))})
        if bid in seen:
            continue
        seen.add(bid)
        if actionable(b):
            kept.append(b)
    return kept, digest


def build_sidecar(bookmarks: list[dict]) -> list[dict]:
    """Build sanitized sidecar records {bookmark_id, text_snippet, url, extracted_idea}.
    'extracted_idea' is a light one-line classification stub for now; the actual
    idea extraction happens later at the proposer step."""
    out = []
    for b in bookmarks:
        text = _scrub(str(b.get("full_text", "")))
        out.append(
            {
                "bookmark_id": str(b.get("id")),
                "text_snippet": text[:300],
                "url": _scrub(str(b.get("url", ""))),
                "extracted_idea": text[:120],
            }
        )
    return out


def write_sidecar(path: Path, records: list[dict]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with path.open("w") as fh:
        for r in records:
            fh.write(json.dumps(r, sort_keys=True) + "\n")
            n += 1
    return n


DEFAULT_X_CDP_URL = "http://127.0.0.1:9333"
DEFAULT_X_PUBLISHER_ROOT = "/home/seb/.local/share/hermes-x-publisher"
_BROWSER_TIMEOUT_SECONDS = 60
_MAX_BOOKMARK_RECORDS = 100
_MAX_BOOKMARK_TEXT_CHARS = 4_000
_MAX_BOOKMARK_URL_CHARS = 512
_MAX_BOOKMARK_ID_CHARS = 128
_MAX_BOOKMARK_STDOUT_BYTES = 512 * 1024
_BROWSER_SCRIPT = r'''
"use strict";

const path = require("node:path");
const cdpUrl = process.env.X_BOOKMARKS_CDP_URL;
const publisherRoot = process.env.X_PUBLISHER_ROOT;
const limit = Number.parseInt(process.env.X_BOOKMARKS_LIMIT || "", 10);
const maxTextChars = Number.parseInt(process.env.X_BOOKMARKS_MAX_TEXT_CHARS || "", 10);
const maxUrlChars = Number.parseInt(process.env.X_BOOKMARKS_MAX_URL_CHARS || "", 10);
const maxIdChars = Number.parseInt(process.env.X_BOOKMARKS_MAX_ID_CHARS || "", 10);

if (!cdpUrl || !publisherRoot || !Number.isInteger(limit) || limit < 0 ||
    !Number.isInteger(maxTextChars) || maxTextChars < 1 ||
    !Number.isInteger(maxUrlChars) || maxUrlChars < 1 ||
    !Number.isInteger(maxIdChars) || maxIdChars < 1) {
  throw new Error("invalid browser harvest configuration");
}

function writeOutput(value) {
  return new Promise((resolve, reject) => {
    process.stdout.write(value, (error) => error ? reject(error) : resolve());
  });
}

function recordsFromArticles(articles, limits) {
  function boundedText(root, maxChars) {
    if (!root?.ownerDocument) return "";
    const showText = root.ownerDocument.defaultView?.NodeFilter?.SHOW_TEXT ?? 4;
    const walker = root.ownerDocument.createTreeWalker(root, showText);
    let text = "";
    while (text.length < maxChars) {
      const node = walker.nextNode();
      if (!node) break;
      const value = typeof node.nodeValue === "string" ? node.nodeValue : "";
      text += value.slice(0, maxChars - text.length);
    }
    return text.trim();
  }

  const output = [];
  for (const article of articles) {
    if (output.length >= limits.limit) break;
    for (const node of article.querySelectorAll("a[href]")) {
      let link;
      try {
        link = new URL(node.getAttribute("href"), "https://x.com");
      } catch {
        continue;
      }
      if (!["x.com", "www.x.com"].includes(link.hostname.toLowerCase())) continue;
      const match = link.pathname.match(/^\/([^/]+)\/status\/([0-9]+)\/?$/);
      if (!match) continue;
      const tweetText = article.querySelector('[data-testid="tweetText"]');
      const id = match[2].slice(0, limits.maxIdChars);
      output.push({
        id,
        full_text: boundedText(tweetText || article, limits.maxTextChars),
        url: `https://x.com/${match[1]}/status/${id}`.slice(0, limits.maxUrlChars),
      });
      break;
    }
  }
  return output;
}

async function run() {
  if (limit === 0) {
    await writeOutput("[]");
    return;
  }

  const { chromium } = require(path.join(publisherRoot, "node_modules", "playwright-core"));
  const browser = await chromium.connectOverCDP(cdpUrl);
  const context = browser.contexts()[0];
  if (!context) throw new Error("persistent browser has no default context");

  const page = await context.newPage();
  try {
    await page.goto("https://x.com/i/bookmarks", {
      waitUntil: "domcontentloaded",
      timeout: 30_000,
    });
    await page.locator("article").first().waitFor({ state: "visible", timeout: 15_000 });

    const records = new Map();
    for (let round = 0; round < 24 && records.size < limit; round += 1) {
      const batch = await page.locator("article").evaluateAll(recordsFromArticles, {
        limit: limit - records.size,
        maxTextChars,
        maxUrlChars,
        maxIdChars,
      });
      for (const record of batch) {
        if (!records.has(record.id)) records.set(record.id, record);
      }
      if (records.size >= limit) break;
      await page.evaluate(() => window.scrollBy(0, Math.max(window.innerHeight * 0.8, 700)));
      await page.waitForTimeout(750);
    }
    if (records.size === 0) throw new Error("no bookmark records found");
    await writeOutput(JSON.stringify([...records.values()].slice(0, limit)));
  } finally {
    await page.close().catch(() => {});
  }
}

run().then(
  () => process.exit(0),
  () => process.exit(1),
);
'''


def _first_env(*names: str, default: str) -> str:
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return default


def _validate_loopback_cdp_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except (TypeError, ValueError) as exc:
        raise RuntimeError("browser CDP URL must be a loopback HTTP endpoint") from exc
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
        or port is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise RuntimeError("browser CDP URL must be a loopback HTTP endpoint")
    return value.rstrip("/")


def _strict_bookmark_records(data, source: str, *, exact: bool = False) -> list[dict]:
    if (
        not isinstance(data, list)
        or len(data) > _MAX_BOOKMARK_RECORDS
        or not all(isinstance(record, dict) for record in data)
    ):
        raise RuntimeError(f"{source} returned an unsupported bookmarks response")
    for record in data:
        bookmark_id = record.get("id")
        if (
            (exact and set(record) != {"id", "full_text", "url"})
            or isinstance(bookmark_id, bool)
            or not isinstance(bookmark_id, (int, str))
            or not str(bookmark_id)
            or len(str(bookmark_id)) > _MAX_BOOKMARK_ID_CHARS
            or not isinstance(record.get("full_text"), str)
            or len(record.get("full_text", "")) > _MAX_BOOKMARK_TEXT_CHARS
            or not isinstance(record.get("url"), str)
            or len(record.get("url", "")) > _MAX_BOOKMARK_URL_CHARS
        ):
            raise RuntimeError(f"{source} returned an invalid bookmark record")
    return data


def _fetch_xurl_bookmarks(n: int) -> list[dict]:
    try:
        proc = run_text_bounded(
            ["xurl", "bookmarks", "-n", str(n)],
            timeout=60,
            max_stdout_bytes=_MAX_BOOKMARK_STDOUT_BYTES,
        )
    except (OSError, subprocess.SubprocessError, OutputLimitExceeded) as exc:
        raise RuntimeError("xurl unavailable") from exc
    if proc.returncode != 0:
        raise RuntimeError(f"xurl bookmarks failed with exit {proc.returncode}")
    try:
        data = json.loads(proc.stdout)
    except (json.JSONDecodeError, TypeError) as exc:
        raise RuntimeError("xurl returned invalid JSON") from exc
    records = data if isinstance(data, list) else None
    # some versions nest under .bookmarks / .data
    if records is None:
        for key in ("bookmarks", "data"):
            if isinstance(data, dict) and isinstance(data.get(key), list):
                records = data[key]
                break
    return _strict_bookmark_records(records, "xurl")[:n]


def _fetch_browser_bookmarks(n: int) -> list[dict]:
    cdp_url = _validate_loopback_cdp_url(
        _first_env(
            "X_BOOKMARKS_CDP_URL",
            "HERMES_X_BOOKMARKS_CDP_URL",
            "HERMES_X_CDP_URL",
            "BROWSER_CDP_URL",
            default=DEFAULT_X_CDP_URL,
        )
    )
    publisher_root = _first_env(
        "X_PUBLISHER_ROOT",
        "HERMES_X_PUBLISHER_ROOT",
        default=DEFAULT_X_PUBLISHER_ROOT,
    )
    # Keep the child environment narrow: no cookies, tokens, or unrelated
    # process secrets are needed to connect to the already-authenticated page.
    env = {
        "PATH": os.environ.get("PATH", ""),
        "X_BOOKMARKS_CDP_URL": cdp_url,
        "X_PUBLISHER_ROOT": publisher_root,
        "X_BOOKMARKS_LIMIT": str(max(0, n)),
        "X_BOOKMARKS_MAX_TEXT_CHARS": str(_MAX_BOOKMARK_TEXT_CHARS),
        "X_BOOKMARKS_MAX_URL_CHARS": str(_MAX_BOOKMARK_URL_CHARS),
        "X_BOOKMARKS_MAX_ID_CHARS": str(_MAX_BOOKMARK_ID_CHARS),
    }
    try:
        proc = run_text_bounded(
            ["node", "-e", _BROWSER_SCRIPT],
            timeout=_BROWSER_TIMEOUT_SECONDS,
            env=env,
            max_stdout_bytes=_MAX_BOOKMARK_STDOUT_BYTES,
        )
    except (OSError, subprocess.SubprocessError, OutputLimitExceeded) as exc:
        raise RuntimeError("browser session unavailable") from exc
    if proc.returncode != 0:
        raise RuntimeError("browser session harvest failed")
    try:
        data = json.loads(proc.stdout)
    except (json.JSONDecodeError, TypeError) as exc:
        raise RuntimeError("browser session returned invalid JSON") from exc
    return _strict_bookmark_records(data, "browser session", exact=True)


def fetch_bookmarks(n: int = 50) -> list[dict]:
    """Read bookmarks via xurl, then a read-only signed-in browser session.

    xurl is always attempted first. Any xurl command, auth, parse, or shape
    failure falls back to bounded Node/Playwright CDP extraction. Neither path
    reads browser cookies/tokens or performs an X write. Errors intentionally
    omit subprocess output so credentials cannot reach logs.
    """
    if isinstance(n, bool) or not isinstance(n, int) or not 0 <= n <= _MAX_BOOKMARK_RECORDS:
        raise RuntimeError(f"bookmark limit must be between 0 and {_MAX_BOOKMARK_RECORDS}")
    try:
        return _fetch_xurl_bookmarks(n)
    except RuntimeError as xurl_error:
        try:
            return _fetch_browser_bookmarks(n)
        except RuntimeError as browser_error:
            raise RuntimeError(f"{xurl_error}; browser session fallback failed") from browser_error


def main(argv) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", required=True, help="Sidecar JSONL output path")
    ap.add_argument("--digest-out", default=None, help="Optional digest JSONL output for noise")
    ap.add_argument("-n", type=int, default=50, help="Number of bookmarks to review")
    args = ap.parse_args(argv[1:] if argv and not argv[0].startswith("-") else argv)

    try:
        bms = fetch_bookmarks(args.n)
    except RuntimeError as exc:
        print(f"RESULT: HALT — {exc}", file=sys.stderr)
        return 1
    if not bms:
        print("RESULT: SKIPPED — no bookmarks returned. No state change.")
        return 0

    kept, digest = partition(actionable=is_actionable, bookmarks=bms)
    recs = build_sidecar(kept)
    n = write_sidecar(Path(args.out), recs)
    if args.digest_out:
        write_sidecar(Path(args.digest_out), digest)

    print(f"RESULT: OK ({len(bms)} reviewed, {n} actionable -> {args.out}, "
          f"{len(digest) - len(kept)} to digest)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))