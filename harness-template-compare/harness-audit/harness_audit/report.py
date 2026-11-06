"""Outputs. One directory, six files:

  report.html    the Confluence page: summary plus the full repo and template tables (see below)
  summary.md     the same summary as Markdown, for a terminal or a pull request
  repos.csv      one row per repo         (inventory: who has Harness, who deploys, rollup per capability)
  stages.csv     one row per deploy stage x capability   (the evidence, with file and line)
  templates.csv  one row per template     (is it used? by whom? who hand-rolls it instead?)
  findings.json  everything above, lossless, for other tools

report.html and summary.md are rendered from the same blocks, so they cannot disagree.

report.html is a *fragment*, not a document: no <html>/<head>/<body>, no <style>, no <script>, no entities
that are not valid XML (so no &nbsp;), every tag closed. That is the subset Confluence storage format
accepts, and it also renders in a browser. Colour is inline style on a <span>.
"""
from __future__ import annotations

import csv
import html
import json
import re
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .analyze import FAILING, SEVERITY, Index, RepoResult, Rules, repo_status

BANNER = "INTERNAL USE ONLY - Via Logic LLC. Not for distribution outside the organization."
STATUS_ORDER = ["compliant", "outdated", "review", "lookalike", "inline", "blocked", "missing"]
MEANING = {
    "compliant": "uses the platform template (current or unpinned version)",
    "outdated": "uses the platform template, but a deprecated version",
    "review": "cannot be decided automatically (template not inspectable, weak hint, unclassified version)",
    "lookalike": "platform template identifier at the wrong scope (forked copy)",
    "inline": "hand-rolled implementation instead of the template",
    "blocked": "uses a version the platform team has blocked",
    "missing": "deploys with no evidence of the capability",
}
_RED, _GREY, _AMBER = ("#FFEBE6", "#BF2600"), ("#F4F5F7", "#42526E"), ("#FFFAE6", "#974F0C")
STATUS_COLORS = {
    "compliant": ("#E3FCEF", "#006644"), "outdated": _AMBER, "review": ("#DEEBFF", "#0747A6"),
    "lookalike": _RED, "inline": _RED, "blocked": ("#FFBDAD", "#BF2600"), "missing": ("#FFBDAD", "#BF2600"),
    "no_deploy": _GREY, "no_harness": _GREY, "parse_error": _AMBER, "fetch_error": _RED,
}
TEMPLATE_COLUMNS = ["scope", "ident", "version", "type", "in_golden", "capability", "repos_direct", "repos_via_wrapper",
                    "pipelines_using", "stable_refs", "repos", "repos_hand_rolling_instead", "path"]
_XML_ILLEGAL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ufffe\uffff]")


# ---------------------------------------------------------------------------
# Blocks: one description of the report, two renderers
# ---------------------------------------------------------------------------

@dataclass
class Block:
    kind: str  # banner | h2 | h3 | p | table
    text: str = ""
    header: list = field(default_factory=list)
    rows: list = field(default_factory=list)
    only: str = ""  # "" = both renderers, "html" or "md" = just that one


def _md_table(header: list, rows: list) -> str:
    if not rows:
        return "_none_\n"
    esc = lambda v: str(v).replace("|", "\\|").replace("\n", " ")
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    lines += ["| " + " | ".join(esc(c) for c in r) + " |" for r in rows]
    return "\n".join(lines) + "\n"


def render_md(blocks: list[Block]) -> str:
    out = ["# Harness template audit", ""]
    for b in blocks:
        if b.only == "html":
            continue
        if b.kind == "banner":
            out += [f"**{b.text}**", ""]
        elif b.kind in ("h2", "h3"):
            out += [("##" if b.kind == "h2" else "###") + f" {b.text}", ""]
        elif b.kind == "p":
            out += [b.text, ""]
        else:
            out += [_md_table(b.header, b.rows)]
    return "\n".join(out)


def _e(value) -> str:
    return html.escape(_XML_ILLEGAL.sub("", str(value)))


def _cell(value) -> str:
    text = _XML_ILLEGAL.sub("", str(value))
    if text in STATUS_COLORS:
        bg, fg = STATUS_COLORS[text]
        return (f'<span style="background-color: {bg}; color: {fg}; padding: 2px 6px; border-radius: 3px;">'
                f"{html.escape(text)}</span>")
    return html.escape(text)


def render_html(blocks: list[Block]) -> str:
    out = []
    for b in blocks:
        if b.only == "md":
            continue
        if b.kind == "banner":
            out.append(f"<p><strong>{_e(b.text)}</strong></p>")
        elif b.kind in ("h2", "h3"):
            out.append(f"<{b.kind}>{_e(b.text)}</{b.kind}>")
        elif b.kind == "p":
            out.append(f"<p>{_e(b.text)}</p>")
        elif not b.rows:
            out.append("<p><em>none</em></p>")
        else:
            head = "".join(f"<th>{_e(h)}</th>" for h in b.header)
            body = "".join("<tr>" + "".join(f"<td>{_cell(c)}</td>" for c in r) + "</tr>" for r in b.rows)
            out.append(f"<table><tbody><tr>{head}</tr>{body}</tbody></table>")
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------------------
# Row builders
# ---------------------------------------------------------------------------

def _csv(path: Path, header: list, rows) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        w.writerows(rows)


def _template_rows(repos: list[RepoResult], checks: list, rules: Rules, golden: Index) -> list[dict]:
    used: dict[tuple, dict] = defaultdict(lambda: {"direct": set(), "via": set(), "pipelines": set(), "stable_refs": 0})
    in_golden = {(t.scope, t.ident, t.version): t for t in golden}
    for r in repos:
        for u in r.uses:
            version = u.version
            if version is None:  # "stable": attribute it to the highest version we know
                t = golden.get(u.scope, u.ident)
                version = t.version if t else ""
            slot = used[(u.scope, u.ident, version)]
            slot["via" if u.via else "direct"].add(r.repo)
            slot["pipelines"].add((r.repo, u.pipeline))
            slot["stable_refs"] += u.version is None
    hand_rolled = defaultdict(set)  # capability -> repos that implement it inline or via a fork
    for c in checks:
        if c.status in ("inline", "lookalike"):
            hand_rolled[c.capability].add(c.repo)
    canonical_of = {(s, r.ident): cap.key for cap in rules.capabilities.values() for r in cap.templates for s in r.scopes}
    rows = []
    for key in sorted(set(in_golden) | set(used)):
        scope, ident, version = key
        t, cap, slot = in_golden.get(key), canonical_of.get((scope, ident), ""), used.get(key)
        rows.append({
            "scope": scope, "ident": ident, "version": version, "type": t.kind if t else "",
            "in_golden": "yes" if t else "no", "capability": cap,
            "repos_direct": len(slot["direct"]) if slot else 0,
            "repos_via_wrapper": len(slot["via"] - slot["direct"]) if slot else 0,
            "pipelines_using": len(slot["pipelines"]) if slot else 0,
            "stable_refs": slot["stable_refs"] if slot else 0,
            "repos": sorted(slot["direct"] | slot["via"]) if slot else [],
            "repos_hand_rolling_instead": sorted(hand_rolled.get(cap, ())) if cap else [],
            "path": t.path if t else "",
        })
    return rows


def _blocks(repos, checks, required, run, tpl_rows) -> list[Block]:
    B: list[Block] = [Block("banner", BANNER)]
    B.append(Block("p", f"Generated {run['generated_at']} | repos scanned: {len(repos)} | coverage rule: {run['coverage']}"))
    for g in run.get("golden", []):
        B.append(Block("p", f"Golden templates: {g.get('repo')} @ {(g.get('commit') or 'unknown')[:12]}"))

    has = [r for r in repos if r.has_harness and not r.fetch_error]
    deploy_repos = [r for r in has if any(p["deploy_stages"] for p in r.pipelines)]
    stage_keys = lambda pred: len({(c.repo, c.pipeline, c.stage_id) for c in checks if pred(c)})
    B += [Block("h2", "Inventory"), Block("table", header=["Measure", "Count"], rows=[
        ["Repos scanned", len(repos)],
        ["Repos with a .harness folder", len(has)],
        ["Repos with at least one deploy stage", len(deploy_repos)],
        ["Deploy stages found", stage_keys(lambda c: c.deploy_kind != "unknown")],
        ["Stages that might deploy but use a template we cannot open", stage_keys(lambda c: c.deploy_kind == "unknown")],
        ["Fetch errors", sum(1 for r in repos if r.fetch_error)],
        ["Files that failed to parse", sum(len(r.parse_errors) for r in repos)],
    ])]

    for cap in required:
        mine = [c for c in checks if c.capability == cap.key]
        repo_counts = Counter(repo_status(r, cap.key) for r in deploy_repos)
        stage_counts = Counter(c.status for c in mine)
        B += [Block("h2", cap.key), Block("p", cap.description),
              Block("table", header=["Status", "Meaning", "Repos", "Deploy stages"],
                    rows=[[s, MEANING[s], repo_counts.get(s, 0), stage_counts.get(s, 0)] for s in STATUS_ORDER])]
        for title, wanted in (("Action required", FAILING), ("Needs a human look", {"review"}), ("Outdated version", {"outdated"})):
            rows = sorted({(c.repo, c.team or "", f"{c.pipeline}:{c.line}", c.stage_id, c.status, "; ".join(c.reasons))
                           for c in mine if c.status in wanted}, key=lambda r: (-SEVERITY[r[4]], r[0], r[2]))
            B += [Block("h3", f"{title} ({len(rows)})"),
                  Block("table", header=["Repo", "Team", "Pipeline:line", "Stage", "Status", "Why"], rows=rows)]

    # Confluence is where people browse, so it carries the full tables; the Markdown points at the CSVs instead.
    B.append(Block("p", "Full tables: repos.csv, stages.csv, templates.csv, findings.json.", only="md"))
    rank = lambda r: max((SEVERITY.get(repo_status(r, c.key), -1) for c in required), default=-1)
    B += [Block("h2", f"All repos ({len(repos)})", only="html"),
          Block("table", only="html", header=["Repo", "Team", "Pipelines", "Deploy stages"] + [c.key for c in required],
                rows=[[r.repo, r.team or "", len(r.pipelines), sum(p["deploy_stages"] for p in r.pipelines)]
                      + [repo_status(r, c.key) for c in required]
                      for r in sorted(repos, key=lambda r: (-rank(r), r.repo))]),
          Block("h2", "Golden template usage", only="html"),
          Block("p", "repos_direct = a pipeline references the template; via wrapper = only through another template. "
                     "Unused templates show 0 and 0.", only="html"),
          Block("table", only="html",
                header=["Template", "Version", "Type", "In golden", "Required for", "Repos direct", "Repos via wrapper", "Repos hand-rolling instead"],
                rows=[[f"{t['scope']}.{t['ident']}", t["version"], t["type"], t["in_golden"], t["capability"],
                       t["repos_direct"], t["repos_via_wrapper"], len(t["repos_hand_rolling_instead"])] for t in tpl_rows])]

    heur = sorted({(c.repo, c.pipeline, c.stage_id, c.deploy_detail or "") for c in checks if c.deploy_kind == "heuristic"})
    B += [Block("h2", f"Deploys detected by command, not stage type ({len(heur)})"),
          Block("p", "These CI/Custom stages run deploy-style commands. Confirm they are real deployments."),
          Block("table", header=["Repo", "Pipeline", "Stage", "Matched"], rows=heur)]
    bad = [(r.repo, e.path, e.message) for r in repos for e in r.parse_errors]
    B += [Block("h2", f"Files that failed to parse ({len(bad)})"), Block("table", header=["Repo", "File", "Error"], rows=bad)]
    errs = [(r.repo, r.fetch_error) for r in repos if r.fetch_error]
    B += [Block("h2", f"Fetch errors ({len(errs)})"), Block("table", header=["Repo", "Error"], rows=errs)]
    none = sorted(r.repo for r in repos if not r.has_harness and not r.fetch_error)
    B += [Block("h2", f"Repos with no .harness folder ({len(none)})"), Block("p", ", ".join(none) or "none")]
    return B


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------

def write_reports(out: Path, repos: list[RepoResult], rules: Rules, golden: Index, run: dict) -> None:
    out.mkdir(parents=True, exist_ok=True)
    checks = [c for r in repos for c in r.checks]
    required = [c for c in rules.capabilities.values() if c.required]
    tpl_rows = _template_rows(repos, checks, rules, golden)

    _csv(out / "stages.csv",
         ["repo", "team", "pipeline", "pipeline_id", "stage_id", "stage_name", "line", "deploy_kind", "deploy_detail",
          "capability", "required", "status", "reasons", "flags", "evidence"],
         [[c.repo, c.team, c.pipeline, c.pipeline_id, c.stage_id, c.stage_name, c.line, c.deploy_kind, c.deploy_detail,
           c.capability, c.required, c.status, "; ".join(c.reasons), ",".join(c.flags), "; ".join(c.evidence)]
          for c in checks])
    _csv(out / "repos.csv",
         ["repo", "team", "branch", "commit", "has_harness", "pipelines", "deploy_stages", "parse_errors", "fetch_error"]
         + [f"status:{c.key}" for c in required],
         [[r.repo, r.team, r.branch, r.commit, r.has_harness, len(r.pipelines), sum(p["deploy_stages"] for p in r.pipelines),
           len(r.parse_errors), r.fetch_error] + [repo_status(r, c.key) for c in required]
          for r in repos])
    _csv(out / "templates.csv", TEMPLATE_COLUMNS,
         [[("; ".join(t[k]) if isinstance(t[k], list) else t[k]) for k in TEMPLATE_COLUMNS] for t in tpl_rows])
    (out / "findings.json").write_text(json.dumps({"run": run, "repos": [asdict(r) for r in repos]}, indent=2, default=str), encoding="utf-8")

    blocks = _blocks(repos, checks, required, run, tpl_rows)
    (out / "summary.md").write_text(render_md(blocks), encoding="utf-8")
    (out / "report.html").write_text(render_html(blocks), encoding="utf-8")


def run_info(rules: Rules, golden_meta: list, snapshot: Path) -> dict:
    return {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "coverage": rules.policy.coverage,
        "golden": golden_meta,
        "snapshot": str(snapshot),
    }
