#!/usr/bin/env python3
"""
Auto-regenerate the "Recent milestones" section of README.md from real
GitHub data: tags/releases + commits matching milestone patterns across
all public repos of the user.

Runs in the GitHub Action `update-milestones.yml` (daily cron).
Uses GITHUB_TOKEN — only public repo data, no scopes needed beyond default.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

USER = "sky1241"
README = Path(__file__).resolve().parents[2] / "README.md"
MAX_ITEMS = 13
MARKER_START = "<!-- MILESTONES:START -->"
MARKER_END = "<!-- MILESTONES:END -->"

# Commit message patterns that signal a real milestone (vs noise commits).
MILESTONE_PATTERNS = [
    r"\bv?\d+\.\d+(\.\d+)?\b",          # version numbers
    r"\brelease[d]?\b",
    r"\bship(ped|ping)?\b",
    r"\bdeploy(ed)?\b",
    r"\bgo[- ]?live\b",
    r"\bmilestone\b",
    r"\bRAPPORT\b",
    r"\bAUDIT_FINAL\b",
    r"\bBUG[+_-]?\d+\b",
    r"\bcycle[- ]?\d+\b",
    r"\bMERGE\b",
]
MILESTONE_RE = re.compile("|".join(MILESTONE_PATTERNS), re.IGNORECASE)

NOISE = re.compile(
    r"^(chore|docs|fix typo|wip|tmp|test:|merge branch|update readme|profile:)",
    re.IGNORECASE,
)

# Don't include commits from the profile repo itself (meta-noise).
SKIP_REPOS = {"sky1241"}

LABEL_MAX = 78


def gh(args: list[str]) -> str:
    """Run gh CLI and return stdout. Empty string on failure."""
    try:
        r = subprocess.run(
            ["gh", *args],
            capture_output=True,
            text=True,
            check=True,
            timeout=60,
        )
        return r.stdout
    except subprocess.CalledProcessError as e:
        print(f"gh {args}: {e.stderr}", file=sys.stderr)
        return ""


def list_repos() -> list[str]:
    out = gh(
        [
            "repo", "list", USER,
            "--limit", "100",
            "--no-archived",
            "--visibility", "public",
            "--json", "name",
        ]
    )
    if not out:
        return []
    return [r["name"] for r in json.loads(out)]


def fetch_releases(repo: str) -> list[dict]:
    out = gh(
        [
            "api",
            f"/repos/{USER}/{repo}/releases?per_page=10",
        ]
    )
    if not out:
        return []
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return []
    items = []
    for r in data:
        if r.get("draft") or r.get("prerelease"):
            continue
        items.append(
            {
                "date": r["published_at"][:10],
                "repo": repo,
                "kind": "release",
                "label": f"{repo} {r['tag_name']} released",
            }
        )
    return items


def fetch_tags(repo: str) -> list[dict]:
    """Fallback for repos without GH releases — list tags via git refs."""
    out = gh(["api", f"/repos/{USER}/{repo}/tags?per_page=10"])
    if not out:
        return []
    try:
        tags = json.loads(out)
    except json.JSONDecodeError:
        return []
    items = []
    for t in tags[:5]:
        sha = t["commit"]["sha"]
        c = gh(["api", f"/repos/{USER}/{repo}/commits/{sha}"])
        if not c:
            continue
        try:
            commit = json.loads(c)
        except json.JSONDecodeError:
            continue
        date = commit["commit"]["author"]["date"][:10]
        items.append(
            {
                "date": date,
                "repo": repo,
                "kind": "tag",
                "label": f"{repo} {t['name']} tagged",
            }
        )
    return items


def fetch_commits(repo: str) -> list[dict]:
    out = gh(
        [
            "api",
            f"/repos/{USER}/{repo}/commits?per_page=30",
        ]
    )
    if not out:
        return []
    try:
        commits = json.loads(out)
    except json.JSONDecodeError:
        return []
    items = []
    for c in commits:
        msg = c["commit"]["message"].split("\n", 1)[0]
        if NOISE.match(msg):
            continue
        if not MILESTONE_RE.search(msg):
            continue
        date = c["commit"]["author"]["date"][:10]
        items.append(
            {
                "date": date,
                "repo": repo,
                "kind": "commit",
                "label": f"{repo} · {msg[:80]}",
            }
        )
    return items


def dedupe_keep_best(items: list[dict]) -> list[dict]:
    """At most one entry per (date, repo). Prefer release > tag > commit."""
    rank = {"release": 0, "tag": 1, "commit": 2}
    by_key: dict[tuple[str, str], dict] = {}
    for it in items:
        key = (it["date"], it["repo"])
        cur = by_key.get(key)
        if cur is None or rank[it["kind"]] < rank[cur["kind"]]:
            by_key[key] = it
    return list(by_key.values())


def cap_per_repo(items: list[dict], max_per_repo: int = 2) -> list[dict]:
    """Keep at most N entries per repo so the timeline isn't dominated by one."""
    by_repo: dict[str, int] = {}
    out = []
    for it in items:
        n = by_repo.get(it["repo"], 0)
        if n >= max_per_repo:
            continue
        by_repo[it["repo"]] = n + 1
        out.append(it)
    return out


def truncate(label: str, limit: int = LABEL_MAX) -> str:
    if len(label) <= limit:
        return label
    return label[: limit - 1].rstrip() + "…"


def collect_all() -> list[dict]:
    repos = [r for r in list_repos() if r not in SKIP_REPOS]
    print(f"Found {len(repos)} repos (after skip): {repos}", file=sys.stderr)
    items: list[dict] = []
    for r in repos:
        items.extend(fetch_releases(r))
        items.extend(fetch_tags(r))
        items.extend(fetch_commits(r))
    return items


def render(items: list[dict]) -> str:
    items = dedupe_keep_best(items)
    items.sort(key=lambda x: x["date"], reverse=True)
    items = cap_per_repo(items, max_per_repo=2)
    items = items[:MAX_ITEMS]

    lines = ["```"]
    for it in items:
        lines.append(f"{it['date']}  ─  {truncate(it['label'])}")
    # Pin the origin line at the bottom for narrative.
    lines.append("2026-01-01  ─  Started writing code seriously · self-taught from zero")
    lines.append("```")
    return "\n".join(lines)


def splice_readme(new_block: str) -> bool:
    text = README.read_text(encoding="utf-8")
    if MARKER_START not in text or MARKER_END not in text:
        # First run — insert markers around the existing block.
        pattern = re.compile(
            r"(## 🏆 Recent milestones\n\n)```\n.*?\n```",
            re.DOTALL,
        )
        new_text = pattern.sub(
            rf"\1{MARKER_START}\n{new_block}\n{MARKER_END}",
            text,
            count=1,
        )
        if new_text == text:
            print("Could not locate milestones block. README untouched.", file=sys.stderr)
            return False
    else:
        pattern = re.compile(
            re.escape(MARKER_START) + r".*?" + re.escape(MARKER_END),
            re.DOTALL,
        )
        new_text = pattern.sub(
            f"{MARKER_START}\n{new_block}\n{MARKER_END}",
            text,
        )

    if new_text == text:
        print("No changes to README.", file=sys.stderr)
        return False

    README.write_text(new_text, encoding="utf-8")
    print("README updated.", file=sys.stderr)
    return True


def main() -> int:
    if not os.environ.get("GH_TOKEN") and not os.environ.get("GITHUB_TOKEN"):
        print("No GH_TOKEN / GITHUB_TOKEN in env.", file=sys.stderr)
        return 2
    items = collect_all()
    if not items:
        print("No items collected — aborting (likely transient API issue).", file=sys.stderr)
        return 1
    block = render(items)
    print(block, file=sys.stderr)
    changed = splice_readme(block)
    return 0 if changed else 0  # exit 0 even on no-op — let the action skip commit


if __name__ == "__main__":
    sys.exit(main())
