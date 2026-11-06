"""Run with:  python -m unittest discover -s tests -v

Pipelines and templates are built as Python dicts and dumped to YAML, so each test reads
as "this structure in, that status out" rather than as a wall of YAML.
"""
import base64
import csv
import json
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

import yaml

from harness_audit.__main__ import main
from harness_audit.analyze import (RulesError, analyze_repo, build_catalog, index_templates, load_rules,
                                   repo_status, scan_snapshot)
from harness_audit.fetch import fetch_repo
from harness_audit.report import STATUS_COLORS, render_html
from harness_audit.yamlio import load_docs

RULES = """
policy:
  coverage: same_or_later_stage
  deploy_commands: ['\\bhelm\\s+(upgrade|install)\\b']
capabilities:
  nr_deployment_marker:
    description: marker
    templates:
      - ident: NR_Deployment_Marker
        scopes: [account]
        versions: {current: [v2.0.0], deprecated: [v1.0.0], blocked: [v0.9.0]}
    strong:
      - {name: mutation, regex: 'changeTrackingCreateDeployment'}
      - {name: rest, regex: 'v2/applications/.+/deployments\\.json'}
    weak:
      - {name: wording, regex: 'deployment[ _-]?marker'}
"""
CAP = "nr_deployment_marker"
CANON = "account.NR_Deployment_Marker"

MARKER_SCRIPT = ('curl -s https://api.newrelic.com/graphql -d \'{"query":"mutation { '
                 'changeTrackingCreateDeployment(deployment: {entityGuid: \\"g\\"}) { deploymentId } }"}\'')


# -- builders ------------------------------------------------------------------------
def shell(script, sid="s1"):
    return {"step": {"type": "ShellScript", "name": sid, "identifier": sid,
                     "spec": {"source": {"type": "Inline", "spec": {"script": script}}}}}


def use(ref, version=None, sid="marker", **extra):
    t = {"templateRef": ref}
    if version:
        t["versionLabel"] = version
    return {"step": {"name": sid, "identifier": sid, "template": t, **extra}}


def deploy(steps=(), rollback=(), sid="deploy"):
    return {"stage": {"name": sid, "identifier": sid, "type": "Deployment", "spec": {
        "deploymentType": "Kubernetes", "execution": {"steps": list(steps), "rollbackSteps": list(rollback)}}}}


def custom(steps, sid="post"):
    return {"stage": {"name": sid, "identifier": sid, "type": "Custom", "spec": {"execution": {"steps": list(steps)}}}}


def ci(steps, sid="build"):
    return {"stage": {"name": sid, "identifier": sid, "type": "CI", "spec": {"execution": {"steps": list(steps)}}}}


def stage_from(ref, version="v1", sid="deploy"):
    return {"stage": {"name": sid, "identifier": sid, "template": {"templateRef": ref, "versionLabel": version}}}


def pipeline(stages, ident="p"):
    return {"pipeline": {"name": ident, "identifier": ident, "stages": list(stages)}}


def template(ident, version, kind, spec, **extra):
    return {"template": {"name": ident, "identifier": ident, "versionLabel": version, "type": kind, **extra, "spec": spec}}


def marker_spec():
    return {"type": "ShellScript", "spec": {"source": {"type": "Inline", "spec": {"script": MARKER_SCRIPT}}}}


def read_csv(path: Path) -> list:
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def put(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data if isinstance(data, str) else yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.rules_path = self.tmp / "capabilities.yaml"
        put(self.rules_path, RULES)
        g = self.tmp / "golden" / "platform__golden" / "templates"
        for v in ("v0.9.0", "v1.0.0", "v2.0.0", "v3.0.0"):
            put(g / f"nr_{v}.yaml", template("NR_Deployment_Marker", v, "Step", marker_spec()))
        put(g / "wrapper.yaml", template("Marker_Wrapper", "v1", "Step", {"template": {"templateRef": CANON, "versionLabel": "v2.0.0"}}))
        put(g / "legacy.yaml", template("Old_Marker", "v1", "Step", {
            "type": "ShellScript", "spec": shell("curl -X POST https://api.newrelic.com/v2/applications/1/deployments.json")["step"]["spec"]}))
        put(g / "std_deploy.yaml", template("Std_Deploy", "v1", "Stage", {
            "type": "Deployment", "spec": {"deploymentType": "Kubernetes", "execution": {"steps": [use(CANON, "v2.0.0")]}}}))
        put(g / "bare_deploy.yaml", template("Bare_Deploy", "v1", "Stage", {
            "type": "Deployment", "spec": {"deploymentType": "Kubernetes", "execution": {"steps": [shell("echo hi")]}}}))
        put(g / "std_pipeline.yaml", template("Std_Pipeline", "v1", "Pipeline", {"stages": [deploy([use(CANON, "v2.0.0")])]}))
        put(g / "bare_pipeline.yaml", template("Bare_Pipeline", "v1", "Pipeline", {"stages": [deploy([shell("echo hi")])]}))
        put(g / "cycle_a.yaml", template("Cycle_A", "v1", "Step", {"template": {"templateRef": "account.Cycle_B", "versionLabel": "v1"}}))
        put(g / "cycle_b.yaml", template("Cycle_B", "v1", "Step", {"template": {"templateRef": "account.Cycle_A", "versionLabel": "v1"}}))
        self.rules = load_rules(self.rules_path)
        docs, _ = load_docs(self.tmp / "golden")
        self.golden = index_templates(docs)

    def audit(self, stages=None, files=None, rules=None):
        """Write one team repo and analyze it. Returns the RepoResult."""
        snap = self.tmp / "snap"
        repo = snap / "team__app"
        if stages is not None:
            put(repo / ".harness" / "pipelines" / "p.yaml", pipeline(stages))
        for rel, data in (files or {}).items():
            put(repo / ".harness" / rel, data)
        repo.mkdir(parents=True, exist_ok=True)
        put(repo / "_meta.json", json.dumps({"team": "T", "repo": "team/app", "commit": "abc", "branch": "main"}))
        return analyze_repo(rules or self.rules, self.golden, repo)

    @staticmethod
    def status(res, sid="deploy"):
        return next(c.status for c in res.checks if c.stage_id == sid and c.capability == CAP)

    @staticmethod
    def check(res, sid="deploy"):
        return next(c for c in res.checks if c.stage_id == sid and c.capability == CAP)


class Classification(Base):
    def test_current_version_is_compliant(self):
        self.assertEqual(self.status(self.audit([deploy([use(CANON, "v2.0.0")])])), "compliant")

    def test_deprecated_version_is_outdated(self):
        self.assertEqual(self.status(self.audit([deploy([use(CANON, "v1.0.0")])])), "outdated")

    def test_blocked_version(self):
        self.assertEqual(self.status(self.audit([deploy([use(CANON, "v0.9.0")])])), "blocked")

    def test_unclassified_version_needs_review(self):
        self.assertEqual(self.status(self.audit([deploy([use(CANON, "v3.0.0")])])), "review")

    def test_unpinned_is_compliant_but_flagged(self):
        c = self.check(self.audit([deploy([use(CANON)])]))
        self.assertEqual(c.status, "compliant")
        self.assertIn("unpinned_version", c.flags)

    def test_wrapper_step_template_is_compliant_via_wrapper(self):
        c = self.check(self.audit([deploy([use("account.Marker_Wrapper", "v1")])]))
        self.assertEqual(c.status, "compliant")
        self.assertIn("via_wrapper", c.flags)

    def test_stage_template_is_a_deploy_and_carries_the_marker(self):
        res = self.audit([stage_from("account.Std_Deploy")])
        self.assertEqual(self.check(res).deploy_kind, "template")
        self.assertEqual(self.status(res), "compliant")

    def test_stage_template_without_marker_is_missing(self):
        self.assertEqual(self.status(self.audit([stage_from("account.Bare_Deploy")])), "missing")

    def test_legacy_golden_template_is_inline_not_compliant(self):
        self.assertEqual(self.status(self.audit([deploy([use("account.Old_Marker", "v1")])])), "inline")

    def test_hand_rolled_script_is_inline(self):
        self.assertEqual(self.status(self.audit([deploy([shell(MARKER_SCRIPT)])])), "inline")

    def test_weak_hint_alone_needs_review(self):
        self.assertEqual(self.status(self.audit([deploy([shell('echo "TODO deployment marker"')])])), "review")

    def test_unresolvable_template_needs_review_not_missing(self):
        c = self.check(self.audit([deploy([use("account.Mystery_Step", "v1")])]))
        self.assertEqual(c.status, "review")
        self.assertIn("Mystery_Step", " ".join(c.reasons))

    def test_canonical_identifier_at_wrong_scope_is_lookalike(self):
        self.assertEqual(self.status(self.audit([deploy([use("NR_Deployment_Marker", "v2.0.0")])])), "lookalike")

    def test_nothing_at_all_is_missing(self):
        self.assertEqual(self.status(self.audit([deploy([shell("echo deploy")])])), "missing")

    def test_template_cycles_terminate(self):
        self.assertEqual(self.status(self.audit([deploy([use("account.Cycle_A", "v1")])])), "missing")


class Structure(Base):
    def test_marker_only_in_rollback_does_not_count(self):
        res = self.audit([deploy(steps=[shell("echo hi")], rollback=[use(CANON, "v2.0.0")])])
        self.assertEqual(self.status(res), "missing")

    def test_disabled_marker_step_does_not_count(self):
        step = use(CANON, "v2.0.0", when={"stageStatus": "Success", "condition": "false"})
        self.assertEqual(self.status(self.audit([deploy([step])])), "missing")

    def test_marker_in_a_later_stage_counts_by_default(self):
        res = self.audit([deploy([shell("echo hi")]), custom([use(CANON, "v2.0.0")])])
        self.assertEqual(self.status(res), "compliant")

    def test_marker_in_an_earlier_stage_does_not_count(self):
        res = self.audit([custom([use(CANON, "v2.0.0")]), deploy([shell("echo hi")])])
        self.assertEqual(self.status(res), "missing")

    def test_same_stage_policy_is_stricter(self):
        strict = load_rules(self._write_rules(RULES.replace("same_or_later_stage", "same_stage")))
        res = self.audit([deploy([shell("echo hi")]), custom([use(CANON, "v2.0.0")])], rules=strict)
        self.assertEqual(self.status(res), "missing")

    def test_parallel_stages_are_flattened(self):
        par = {"parallel": [deploy([use(CANON, "v2.0.0")], sid="a"), deploy([shell("x")], sid="b")]}
        res = self.audit([par])
        self.assertEqual(self.status(res, "a"), "compliant")

    def test_each_deploy_stage_is_judged_separately(self):
        res = self.audit([deploy([use(CANON, "v2.0.0")], sid="a"), custom([shell("x")]), deploy([shell("y")], sid="b")])
        self.assertEqual((self.status(res, "a"), self.status(res, "b")), ("compliant", "missing"))

    def test_unresolvable_stage_template_is_unclassified_not_a_deploy(self):
        res = self.audit([stage_from("account.Mystery_Stage")])
        self.assertEqual(self.check(res).deploy_kind, "unknown")
        self.assertEqual((res.pipelines[0]["deploy_stages"], res.pipelines[0]["unclassified_stages"]), (0, 1))

    def test_helm_in_a_ci_stage_is_a_heuristic_deploy(self):
        res = self.audit([ci([shell("helm upgrade --install app ./chart")])])
        c = self.check(res, "build")
        self.assertEqual((c.deploy_kind, c.status), ("heuristic", "missing"))

    def test_image_build_is_not_a_deploy(self):
        res = self.audit([ci([shell("docker build -t app .")])])
        self.assertEqual(res.checks, [])
        self.assertEqual(repo_status(res, CAP), "no_deploy")

    def test_pipeline_template_with_marker(self):
        res = self.audit(files={"pipelines/pt.yaml": {"pipeline": {"name": "x", "identifier": "x",
                               "template": {"templateRef": "account.Std_Pipeline", "versionLabel": "v1"}}}})
        self.assertEqual(self.status(res, "(pipeline template)"), "compliant")

    def test_pipeline_template_without_marker(self):
        res = self.audit(files={"pipelines/pt.yaml": {"pipeline": {"name": "x", "identifier": "x",
                               "template": {"templateRef": "account.Bare_Pipeline", "versionLabel": "v1"}}}})
        self.assertEqual(self.status(res, "(pipeline template)"), "missing")

    def test_unknown_pipeline_template_needs_review(self):
        res = self.audit(files={"pipelines/pt.yaml": {"pipeline": {"name": "x", "identifier": "x",
                               "template": {"templateRef": "account.Ghost", "versionLabel": "v1"}}}})
        self.assertEqual(self.status(res, "(pipeline template)"), "review")

    def test_repo_local_wrapper_around_the_canonical_template_is_compliant(self):
        local = template("Team_Marker", "v1", "Step", {"template": {"templateRef": CANON, "versionLabel": "v2.0.0"}},
                         projectIdentifier="proj", orgIdentifier="org")
        res = self.audit([deploy([use("Team_Marker", "v1")])], files={"templates/team_marker.yaml": local})
        c = self.check(res)
        self.assertEqual(c.status, "compliant")
        self.assertIn("via_wrapper", c.flags)

    def test_line_numbers_point_at_the_stage(self):
        res = self.audit([deploy([shell("x")])])
        self.assertGreater(self.check(res).line, 0)


class Repos(Base):
    def test_no_harness_folder(self):
        res = self.audit()
        self.assertEqual(repo_status(res, CAP), "no_harness")

    def test_broken_yaml_is_reported_not_raised(self):
        res = self.audit([deploy([use(CANON, "v2.0.0")])], files={"pipelines/broken.yaml": "pipeline: [unclosed\n  - x: {"})
        self.assertEqual(len(res.parse_errors), 1)
        self.assertEqual(self.status(res), "compliant")

    def test_only_unparseable_files_is_parse_error(self):
        res = self.audit(files={"pipelines/broken.yaml": "a: [\n"})
        self.assertEqual(repo_status(res, CAP), "parse_error")

    def test_fetch_error_from_meta(self):
        repo = self.tmp / "snap" / "x__y"
        put(repo / "_meta.json", json.dumps({"repo": "x/y", "error": "repository not found"}))
        res = analyze_repo(self.rules, self.golden, repo)
        self.assertEqual(repo_status(res, CAP), "fetch_error")


class Rules(Base):
    def _write_rules(self, text):
        p = self.tmp / "r.yaml"
        put(p, text)
        return p

    def test_bad_regex_is_a_clear_error(self):
        with self.assertRaises(RulesError):
            load_rules(self._write_rules(RULES.replace("changeTrackingCreateDeployment", "(unclosed")))

    def test_bad_coverage_is_rejected(self):
        with self.assertRaises(RulesError):
            load_rules(self._write_rules(RULES.replace("same_or_later_stage", "sometimes")))

    def test_catalog_flags_unclassified_and_missing_versions(self):
        rules = load_rules(self._write_rules(RULES.replace("v2.0.0]", "v2.0.0, v9.9.9]")))
        drift = " ".join(build_catalog(rules, self.golden)["drift"])
        self.assertIn("v9.9.9", drift)  # listed but absent from golden
        self.assertIn("v3.0.0", drift)  # in golden but never classified

    def test_deleting_a_deprecated_version_from_golden_is_not_drift(self):
        drift = " ".join(build_catalog(self.rules, self.golden)["drift"])
        self.assertNotIn("v1.0.0 as current", drift)
        self.assertNotIn("v0.9.0", drift)

    def test_template_names_are_not_mistaken_for_keyword_hints(self):
        rows = {(t["ident"], t["version"]): t for t in build_catalog(self.rules, self.golden)["templates"]}
        self.assertFalse([e for e in rows[("Marker_Wrapper", "v1")]["evidence"] if e.startswith("weak")])

    def test_catalog_lists_legacy_implementations_as_non_canonical(self):
        rows = {(t["ident"], t["version"]): t for t in build_catalog(self.rules, self.golden)["templates"]}
        self.assertEqual(rows[("NR_Deployment_Marker", "v2.0.0")]["canonical_for"], [CAP])
        self.assertEqual(rows[("Old_Marker", "v1")]["canonical_for"], [])
        self.assertIn(CAP, rows[("Old_Marker", "v1")]["implements"])
        self.assertIn("changeTrackingCreateDeployment", " ".join(
            rows[("NR_Deployment_Marker", "v2.0.0")]["suggested_keywords"]["graphql_mutations"]))


Base._write_rules = Rules._write_rules
Structure._write_rules = Rules._write_rules


class FakeGitHub:
    def __init__(self, tree_has_harness=True):
        self.has = tree_has_harness

    def get(self, path):
        root = [{"path": "README.md", "type": "blob", "sha": "B0"}]
        if self.has:
            root.append({"path": ".harness", "type": "tree", "sha": "T1"})
        table = {
            "repos/o/r": {"default_branch": "main"},
            "repos/o/r/branches/main": {"commit": {"sha": "abc123"}},
            "repos/o/r/git/trees/main": {"tree": root},
            "repos/o/r/git/trees/T1?recursive=1": {"tree": [
                {"path": "pipelines/p.yaml", "type": "blob", "sha": "B1", "size": 13},
                {"path": "pipelines/notes.md", "type": "blob", "sha": "B2", "size": 3},
                {"path": "pipelines", "type": "tree", "sha": "T2"}]},
            "repos/o/r/git/blobs/B1": {"encoding": "base64", "content": base64.b64encode(b"pipeline: {}\n").decode()},
        }
        return table.get(path)


class Fetch(Base):
    def test_downloads_only_yaml_under_harness_and_writes_meta(self):
        meta = fetch_repo(FakeGitHub(), {"team": "T", "repo": "o/r"}, self.tmp / "out", [".harness"])
        dest = self.tmp / "out" / "o__r"
        self.assertEqual((meta["files"], meta["commit"], meta["error"]), (1, "abc123", None))
        self.assertTrue((dest / ".harness" / "pipelines" / "p.yaml").is_file())
        self.assertFalse((dest / ".harness" / "pipelines" / "notes.md").exists())
        self.assertEqual(json.loads((dest / "_meta.json").read_text())["team"], "T")

    def test_repo_without_harness_is_recorded_not_an_error(self):
        meta = fetch_repo(FakeGitHub(False), {"team": "T", "repo": "o/r"}, self.tmp / "out", [".harness"])
        self.assertEqual((meta["has_harness"], meta["error"]), (False, None))

    def test_missing_repo_is_recorded_as_an_error(self):
        meta = fetch_repo(FakeGitHub(), {"team": "T", "repo": "o/missing"}, self.tmp / "out", [".harness"])
        self.assertIn("not found", meta["error"])
        self.assertTrue((self.tmp / "out" / "o__missing" / "_meta.json").is_file())


class EndToEnd(Base):
    def test_scan_writes_all_reports_and_exit_codes(self):
        for name, stages in (("a__good", [deploy([use(CANON, "v2.0.0")])]), ("b__bad", [deploy([shell("echo hi")])])):
            repo = self.tmp / "snap" / name
            put(repo / ".harness" / "pipelines" / "p.yaml", pipeline(stages))
            put(repo / "_meta.json", json.dumps({"team": name[0], "repo": name.replace("__", "/")}))
        out = self.tmp / "report"
        args = ["scan", "--snapshot", str(self.tmp / "snap"), "--golden", str(self.tmp / "golden"),
                "--rules", str(self.rules_path), "--out", str(out)]
        self.assertEqual(main(args), 0)
        self.assertEqual(main(args + ["--fail-on", "fail"]), 2)
        for f in ("report.html", "summary.md", "repos.csv", "stages.csv", "templates.csv", "findings.json"):
            self.assertTrue((out / f).is_file(), f)
        rows = {r["repo"]: r for r in read_csv(out / "repos.csv")}
        self.assertEqual(rows["a/good"][f"status:{CAP}"], "compliant")
        self.assertEqual(rows["b/bad"][f"status:{CAP}"], "missing")
        tpl = {(r["ident"], r["version"]): r for r in read_csv(out / "templates.csv")}
        self.assertEqual(tpl[("NR_Deployment_Marker", "v2.0.0")]["repos_direct"], "1")
        self.assertEqual(tpl[("NR_Deployment_Marker", "v1.0.0")]["repos_direct"], "0")  # published but unused
        self.assertIn("INTERNAL USE ONLY", (out / "summary.md").read_text())

    def test_usage_through_a_wrapper_still_counts_for_the_inner_template(self):
        repo = self.tmp / "snap" / "a__via"
        put(repo / ".harness" / "pipelines" / "p.yaml", pipeline([stage_from("account.Std_Deploy")]))
        put(repo / "_meta.json", json.dumps({"team": "a", "repo": "a/via"}))
        out = self.tmp / "report"
        main(["scan", "--snapshot", str(self.tmp / "snap"), "--golden", str(self.tmp / "golden"),
              "--rules", str(self.rules_path), "--out", str(out)])
        tpl = {(r["ident"], r["version"]): r for r in read_csv(out / "templates.csv")}
        row = tpl[("NR_Deployment_Marker", "v2.0.0")]
        self.assertEqual((row["repos_direct"], row["repos_via_wrapper"]), ("0", "1"))
        self.assertEqual(tpl[("Std_Deploy", "v1")]["repos_direct"], "1")

    def test_catalog_command_exit_code_reflects_drift(self):
        args = ["catalog", "--golden", str(self.tmp / "golden"), "--rules", str(self.rules_path), "--out", str(self.tmp / "cat")]
        self.assertEqual(main(args), 1)  # v3.0.0 exists in golden but is unclassified
        self.assertTrue((self.tmp / "cat" / "catalog.json").is_file())


class ConfluenceHtml(Base):
    """report.html is handed to the Confluence publish template, which stores it as storage-format XHTML."""

    def scan_to_html(self):
        for name, team, stages in (("a__good", "Payments & <Core>", [deploy([use(CANON, "v2.0.0")])]),
                                   ("b__bad", "Search", [deploy([shell("echo hi")])]),
                                   ("c__hostile", "<script>alert(1)</script>", [deploy([shell("x")])])):
            repo = self.tmp / "snap" / name
            put(repo / ".harness" / "pipelines" / "p.yaml", pipeline(stages))
            put(repo / "_meta.json", json.dumps({"team": team, "repo": name.replace("__", "/") + "<b>\u0001"}))
        out = self.tmp / "report"
        main(["scan", "--snapshot", str(self.tmp / "snap"), "--golden", str(self.tmp / "golden"),
              "--rules", str(self.rules_path), "--out", str(out)])
        return (out / "report.html").read_text(encoding="utf-8"), (out / "summary.md").read_text(encoding="utf-8")

    def test_is_a_well_formed_xml_fragment_even_with_hostile_names(self):
        page, _ = self.scan_to_html()
        ET.fromstring("<root>" + page + "</root>")  # raises on any unclosed tag or illegal entity or control character

    def test_contains_nothing_confluence_strips_or_rejects(self):
        page, _ = self.scan_to_html()
        for banned in ("<html", "<body", "<head", "<style", "<script", "<details", "&nbsp;", "<link", "<img"):
            self.assertNotIn(banned, page)

    def test_hostile_text_is_escaped_not_interpreted(self):
        page, _ = self.scan_to_html()
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", page)
        self.assertIn("Payments &amp; &lt;Core&gt;", page)

    def test_statuses_are_colour_coded_and_every_status_has_a_colour(self):
        page, _ = self.scan_to_html()
        self.assertRegex(page, r'<span style="background-color: #[0-9A-F]{6}; color: #[0-9A-F]{6};[^"]*">compliant</span>')
        self.assertRegex(page, r'<span style="[^"]*">missing</span>')
        for status in ("compliant", "outdated", "review", "lookalike", "inline", "blocked", "missing",
                       "no_deploy", "no_harness", "parse_error", "fetch_error"):
            self.assertIn(status, STATUS_COLORS)

    def test_carries_the_full_tables_that_the_markdown_leaves_to_the_csvs(self):
        page, md = self.scan_to_html()
        self.assertIn("All repos (3)", page)
        self.assertIn("Golden template usage", page)
        self.assertNotIn("All repos", md)
        self.assertIn("Full tables: repos.csv", md)

    def test_html_and_markdown_agree_on_the_findings(self):
        page, md = self.scan_to_html()
        for heading in ("Action required (2)", "Needs a human look (0)", "Outdated version (0)"):
            self.assertIn(heading, page)
            self.assertIn(heading, md)

    def test_empty_report_still_renders(self):
        page = render_html([])
        self.assertEqual(page.strip(), "")


if __name__ == "__main__":
    unittest.main()
