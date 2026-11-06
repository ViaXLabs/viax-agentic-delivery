"""harness_audit: verify that deploy pipelines use the platform team's Harness templates.

  fetch    download each repo's .harness folder (or the golden repo's template folder)
  catalog  inventory the golden templates and check capabilities.yaml against them
  scan     audit a snapshot of team repos and write the reports
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .analyze import FAILING, RulesError, build_catalog, index_templates, load_rules, scan_snapshot
from .fetch import FetchError, run_fetch
from .report import run_info, write_reports
from .yamlio import load_docs


def _load_golden(golden: Path):
    docs, errors = load_docs(golden)
    index = index_templates(docs)
    for e in errors:
        print(f"golden: could not parse {e.path}: {e.message}", file=sys.stderr)
    if not len(index):
        print("golden: no templates found; every wrapper template will be reported as unresolved", file=sys.stderr)
    meta = []
    for m in sorted(golden.glob("*/_meta.json")):
        data = json.loads(m.read_text(encoding="utf-8"))
        meta.append({"repo": data.get("repo"), "commit": data.get("commit")})
    return index, meta


def cmd_fetch(a) -> int:
    run_fetch(a.repos, Path(a.out), a.path or [".harness"], a.jobs)
    return 0


def cmd_catalog(a) -> int:
    rules = load_rules(a.rules)
    index, _ = _load_golden(Path(a.golden))
    cat = build_catalog(rules, index)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "catalog.json").write_text(json.dumps(cat, indent=2), encoding="utf-8")
    print(f"{len(cat['templates'])} template versions in the golden repo\n")
    for t in cat["templates"]:
        mark = "CANONICAL " if t["canonical_for"] else ""
        impl = f" implements: {', '.join(t['implements'])}" if t["implements"] else ""
        print(f"  {t['scope']}.{t['ident']}@{t['version']} [{t['type']}] {mark}{impl}")
    stray = [t for t in cat["templates"] if t["implements"] and not t["canonical_for"]]
    if stray:
        print("\nTemplates that implement a capability but are not canonical (legacy implementations or wrappers):")
        for t in stray:
            print(f"  {t['scope']}.{t['ident']}@{t['version']}: {', '.join(t['evidence'])}")
    print("\nDrift between capabilities.yaml and the golden repo:")
    print("\n".join(f"  ! {d}" for d in cat["drift"]) or "  none")
    return 1 if cat["drift"] else 0


def cmd_scan(a) -> int:
    rules = load_rules(a.rules)
    index, golden_meta = _load_golden(Path(a.golden))
    repos = scan_snapshot(rules, index, Path(a.snapshot))
    write_reports(Path(a.out), repos, rules, index, run_info(rules, golden_meta, Path(a.snapshot)))
    checks = [c for r in repos for c in r.checks if c.required]
    failing = sum(1 for c in checks if c.status in FAILING)
    review = sum(1 for c in checks if c.status == "review")
    print(f"{len(repos)} repos, {len(checks)} required checks: {failing} failing, {review} need review. Reports in {a.out}/")
    if a.fail_on == "fail" and failing:
        return 2
    if a.fail_on == "review" and (failing or review):
        return 2
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="harness_audit", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    f = sub.add_parser("fetch", help="download .harness folders from GitHub")
    f.add_argument("--repos", required=True, help="teams.json")
    f.add_argument("--out", required=True, help="snapshot directory to create")
    f.add_argument("--path", action="append", help="folder to download (repeatable, default .harness)")
    f.add_argument("--jobs", type=int, default=8)
    f.set_defaults(fn=cmd_fetch)

    c = sub.add_parser("catalog", help="inventory the golden templates and detect rules drift")
    c.add_argument("--golden", required=True, help="directory holding the golden repo's templates")
    c.add_argument("--rules", default="capabilities.yaml")
    c.add_argument("--out", default="report")
    c.set_defaults(fn=cmd_catalog)

    s = sub.add_parser("scan", help="audit a snapshot")
    s.add_argument("--snapshot", required=True)
    s.add_argument("--golden", required=True)
    s.add_argument("--rules", default="capabilities.yaml")
    s.add_argument("--out", default="report")
    s.add_argument("--fail-on", choices=("never", "fail", "review"), default="never",
                   help="exit 2 when required checks fail (default never: this is an inventory, not a gate)")
    s.set_defaults(fn=cmd_scan)

    args = ap.parse_args(argv)
    try:
        return args.fn(args)
    except (RulesError, FetchError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
