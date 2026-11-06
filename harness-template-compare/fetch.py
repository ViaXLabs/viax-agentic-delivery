"""Download only the .harness folder of each repo from GitHub (REST API, standard library only).

Writes the snapshot layout that `scan` consumes:

    <out>/<owner>__<repo>/_meta.json
    <out>/<owner>__<repo>/.harness/**.yaml

_meta.json is written for every repo, including failures and repos with no .harness,
so a repo never disappears from the audit silently.

Auth: GH_TOKEN (or GITHUB_TOKEN), a corporate-account token with read access.
GitHub Enterprise Server: set GITHUB_API_URL=https://<host>/api/v3
"""
from __future__ import annotations

import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

MAX_BLOB = 1_000_000


class FetchError(Exception):
    pass


class GitHub:
    def __init__(self, token: str | None = None, api: str | None = None, retries: int = 4):
        self.token = token or os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
        if not self.token:
            raise FetchError("set GH_TOKEN to a corporate GitHub token with read access to the repos")
        self.api = (api or os.environ.get("GITHUB_API_URL") or "https://api.github.com").rstrip("/")
        self.retries = retries

    def get(self, path: str):
        """GET <api>/<path> as JSON. None on 404. Retries rate limits and 5xx."""
        url = f"{self.api}/{path}"
        for attempt in range(self.retries):
            req = urllib.request.Request(url, headers={
                "Authorization": f"Bearer {self.token}", "Accept": "application/vnd.github+json", "User-Agent": "harness-audit"})
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    return json.load(resp)
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    return None
                limited = exc.code == 429 or (exc.code == 403 and exc.headers.get("X-RateLimit-Remaining") == "0")
                if limited:
                    reset = int(exc.headers.get("X-RateLimit-Reset") or 0)
                    wait = float(exc.headers.get("Retry-After") or max(reset - time.time(), 1))
                elif exc.code >= 500:
                    wait = 2 ** attempt
                else:
                    raise FetchError(f"HTTP {exc.code} for {path}") from None
            except (urllib.error.URLError, TimeoutError):
                wait = 2 ** attempt
            time.sleep(min(wait, 120))
        raise FetchError(f"gave up after {self.retries} attempts: {path}")


def load_repo_list(path: str) -> list[dict]:
    """teams.json: [{"team": ..., "repo": "owner/name", "branch": optional}, ...] (a few key aliases accepted)."""
    out, seen = [], set()
    for entry in json.loads(Path(path).read_text(encoding="utf-8")):
        team = entry.get("team") or entry.get("team_name") or entry.get("name")
        repo = entry.get("repo") or entry.get("repository") or entry.get("repo_name") or entry.get("github_repo")
        if not repo:
            print(f"skipping entry without a repo: {entry}", file=sys.stderr)
            continue
        if repo not in seen:
            seen.add(repo)
            out.append({"team": team, "repo": repo, "branch": entry.get("branch")})
    return out


def _subtree_sha(gh, repo: str, branch: str, prefix: str):
    """Walk down one directory level at a time; this never hits the 100k-entry truncation of a recursive root listing."""
    sha = quote(branch, safe="/")
    for seg in prefix.strip("/").split("/"):
        tree = gh.get(f"repos/{repo}/git/trees/{sha}")
        entry = next((e for e in (tree or {}).get("tree", []) if e["path"] == seg and e["type"] == "tree"), None)
        if entry is None:
            return None
        sha = entry["sha"]
    return sha


def _write_under(dest: Path, rel: str, data: bytes) -> None:
    target = (dest / rel).resolve()
    if dest.resolve() not in target.parents:
        raise FetchError(f"refusing to write outside the snapshot: {rel}")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)


def fetch_repo(gh, entry: dict, out: Path, prefixes: list[str]) -> dict:
    repo = entry["repo"]
    dest = out / repo.replace("/", "__")
    dest.mkdir(parents=True, exist_ok=True)
    meta = {"team": entry.get("team"), "repo": repo, "branch": entry.get("branch"), "commit": None,
            "has_harness": False, "files": 0, "warnings": [], "error": None,
            "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    try:
        info = gh.get(f"repos/{repo}")
        if info is None:
            raise FetchError("repository not found, or the token has no access")
        branch = entry.get("branch") or info["default_branch"]
        head = gh.get(f"repos/{repo}/branches/{quote(branch, safe='/')}")
        if head is None:
            raise FetchError(f"branch '{branch}' not found")
        meta.update(branch=branch, commit=head["commit"]["sha"])
        count = 0
        for prefix in prefixes:
            sha = _subtree_sha(gh, repo, branch, prefix)
            if sha is None:
                continue
            tree = gh.get(f"repos/{repo}/git/trees/{sha}?recursive=1") or {}
            if tree.get("truncated"):
                meta["warnings"].append(f"listing under {prefix} was truncated by GitHub")
            for e in tree.get("tree", []):
                if e["type"] != "blob" or not e["path"].lower().endswith((".yaml", ".yml")):
                    continue
                rel = f"{prefix.strip('/')}/{e['path']}"
                if e.get("size", 0) > MAX_BLOB:
                    meta["warnings"].append(f"skipped {rel}: larger than {MAX_BLOB} bytes")
                    continue
                blob = gh.get(f"repos/{repo}/git/blobs/{e['sha']}")
                if blob is None:
                    meta["warnings"].append(f"could not read {rel}")
                    continue
                raw = base64.b64decode(blob["content"]) if blob.get("encoding") == "base64" else blob["content"].encode()
                _write_under(dest, rel, raw)
                count += 1
        meta.update(files=count, has_harness=count > 0)
    except FetchError as exc:
        meta["error"] = str(exc)
    (dest / "_meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return meta


def run_fetch(repos_file: str, out: Path, prefixes: list[str], jobs: int = 8) -> list[dict]:
    gh = GitHub()
    entries = load_repo_list(repos_file)
    out.mkdir(parents=True, exist_ok=True)
    with ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
        metas = list(pool.map(lambda e: fetch_repo(gh, e, out, prefixes), entries))
    for m in metas:
        state = f"ERROR {m['error']}" if m["error"] else f"{m['files']} files" if m["has_harness"] else "no .harness"
        print(f"{m['repo']}: {state}")
    bad = sum(1 for m in metas if m["error"])
    print(f"fetched {len(metas)} repos, {bad} errors", file=sys.stderr)
    return metas
