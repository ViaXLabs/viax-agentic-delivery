"""Core analysis: rules, template index, evidence collection, per-deploy-stage compliance.

The model in one paragraph
--------------------------
A *capability* (e.g. "New Relic deployment marker") is satisfied by one or more
*canonical templates* named in capabilities.yaml. For every deploy stage we collect
*evidence* from the stage and from every template it references (recursively,
through the golden repo and the repo's own templates):

    template  - a reference to a canonical template (direct, or via a wrapper template)
    lookalike - a reference with the canonical identifier but the wrong scope (a fork)
    inline    - a hand-rolled implementation (strong fingerprint matched)
    weak      - a hint only (weak fingerprint matched)

and then reduce the evidence to one status per (deploy stage, capability).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, replace
from pathlib import Path

import yaml

from .yamlio import Doc, ParseError, load_docs

# Worst first. FAILING statuses count as non-compliant; review means "a human must look".
SEVERITY = {"missing": 6, "blocked": 5, "inline": 4, "lookalike": 3, "review": 2, "outdated": 1, "compliant": 0}
FAILING = {"missing", "blocked", "inline", "lookalike"}
COVERAGE_MODES = ("same_stage", "same_or_later_stage", "pipeline")
_LABELED = ("step", "stepGroup", "stage")


# ---------------------------------------------------------------------------
# Rules (capabilities.yaml)
# ---------------------------------------------------------------------------

class RulesError(ValueError):
    pass


@dataclass(frozen=True)
class TemplateRule:
    ident: str
    scopes: frozenset
    current: frozenset
    deprecated: frozenset
    blocked: frozenset

    def version_status(self, version: str | None) -> str:
        if version is None:
            return "unpinned"  # "always use stable": resolves at run time, we cannot see which
        if version in self.blocked:
            return "blocked"
        if version in self.deprecated:
            return "deprecated"
        if version in self.current:
            return "current"
        if self.current or self.deprecated or self.blocked:
            return "unlisted"  # a version the rules file has never classified
        return "current"  # no version policy: every version is fine


@dataclass(frozen=True)
class Capability:
    key: str
    description: str
    required: bool
    templates: tuple
    strong: tuple  # ((name, regex), ...) -> hand-rolled implementation
    weak: tuple  # ((name, regex), ...) -> hint only


@dataclass(frozen=True)
class Policy:
    coverage: str
    deploy_stage_types: frozenset
    deploy_commands: tuple
    count_rollback_steps: bool


@dataclass(frozen=True)
class Rules:
    policy: Policy
    capabilities: dict


def _fingerprint(spec, where):
    try:
        return str(spec["name"]), re.compile(spec["regex"], re.I)
    except (KeyError, TypeError, re.error) as exc:
        raise RulesError(f"{where}: bad fingerprint {spec!r}: {exc}") from None


def load_rules(path) -> Rules:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    pol = raw.get("policy") or {}
    coverage = pol.get("coverage", "same_or_later_stage")
    if coverage not in COVERAGE_MODES:
        raise RulesError(f"policy.coverage must be one of {COVERAGE_MODES}, got {coverage!r}")
    try:
        deploy_commands = tuple(re.compile(p, re.I) for p in pol.get("deploy_commands", []))
    except re.error as exc:
        raise RulesError(f"policy.deploy_commands: {exc}") from None
    policy = Policy(
        coverage,
        frozenset(pol.get("deploy_stage_types", ["Deployment"])),
        deploy_commands,
        bool(pol.get("count_rollback_steps", False)),
    )
    caps = {}
    for key, c in (raw.get("capabilities") or {}).items():
        where = f"capabilities.{key}"
        try:
            templates = tuple(
                TemplateRule(
                    str(t["ident"]),
                    frozenset(t.get("scopes", ["account", "org"])),
                    *(frozenset(map(str, (t.get("versions") or {}).get(k, []))) for k in ("current", "deprecated", "blocked")),
                )
                for t in c.get("templates", [])
            )
        except (KeyError, TypeError) as exc:
            raise RulesError(f"{where}.templates: each entry needs 'ident' ({exc})") from None
        strong = tuple(_fingerprint(s, f"{where}.strong") for s in c.get("strong", []))
        weak = tuple(_fingerprint(s, f"{where}.weak") for s in c.get("weak", []))
        if not templates and not strong:
            raise RulesError(f"{where}: define at least one template or one strong fingerprint")
        caps[key] = Capability(key, " ".join(str(c.get("description", "")).split()), bool(c.get("required", True)), templates, strong, weak)
    if not caps:
        raise RulesError("capabilities.yaml defines no capabilities")
    return Rules(policy, caps)


# ---------------------------------------------------------------------------
# Template index
# ---------------------------------------------------------------------------

def _vkey(v: str):
    return [int(x) for x in re.findall(r"\d+", v)] or [-1], v


@dataclass(frozen=True)
class Tmpl:
    scope: str  # account | org | project
    ident: str
    version: str
    kind: str  # Step | StepGroup | Stage | Pipeline | ...
    name: str
    description: str
    path: str
    line: int
    spec: object


class Index:
    """All templates known to one source (the golden repo, or one team repo)."""

    def __init__(self):
        self._t: dict[tuple[str, str], dict[str, Tmpl]] = {}

    def add(self, t: Tmpl) -> None:
        self._t.setdefault((t.scope, t.ident), {})[t.version] = t

    def get(self, scope: str, ident: str, version: str | None = None) -> Tmpl | None:
        versions = self._t.get((scope, ident))
        if not versions:
            return None
        # No version in the reference means "stable". The stable flag is not in the YAML,
        # so the highest version label is the best available approximation.
        return versions.get(version) if version else versions[max(versions, key=_vkey)]

    def versions(self, scope: str, ident: str) -> list[str]:
        return sorted(self._t.get((scope, ident), {}), key=_vkey)

    def __iter__(self):
        return (t for versions in self._t.values() for t in versions.values())

    def __len__(self):
        return sum(len(v) for v in self._t.values())


def index_templates(docs: list[Doc], index: Index | None = None) -> Index:
    index = index if index is not None else Index()
    for d in docs:
        if d.kind != "template":
            continue
        t = d.body["template"]
        ident = t.get("identifier")
        if not ident:
            continue
        scope = "project" if t.get("projectIdentifier") else "org" if t.get("orgIdentifier") else "account"
        index.add(Tmpl(
            scope, str(ident), str(t.get("versionLabel") or ""), str(t.get("type") or ""),
            str(t.get("name") or ident), " ".join(str(t.get("description") or "").split()),
            d.path, getattr(t, "line", 0), t.get("spec"),
        ))
    return index


# ---------------------------------------------------------------------------
# Walking Harness YAML
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Ref:
    raw: str
    scope: str
    ident: str
    version: str | None

    @property
    def shown(self) -> str:
        return f"{self.raw}@{self.version or 'stable'}"


def parse_ref(d: dict) -> Ref:
    raw = str(d.get("templateRef", "")).strip()
    if raw.startswith("account."):
        scope, ident = "account", raw[len("account."):]
    elif raw.startswith("org."):
        scope, ident = "org", raw[len("org."):]
    else:
        scope, ident = "project", raw
    v = d.get("versionLabel")
    return Ref(raw, scope, ident, str(v).strip() if v not in (None, "") else None)


def _disabled(node: dict) -> bool:
    when = node.get("when")
    return isinstance(when, dict) and str(when.get("condition", "")).strip().lower() in {"false", "<+false>"}


def walk(node, label=None, line=0, include_rollback=False):
    """Yield (node, label, line) for every mapping and every string under *node*.

    label = nearest enclosing step/stage identifier, line = nearest enclosing mapping line.
    Steps disabled with a literal-false `when` condition are skipped, and so are
    rollbackSteps (a marker that only fires on rollback is not a deployment marker).
    """
    if isinstance(node, str):
        yield node, label, line
    elif isinstance(node, dict):
        if _disabled(node):
            return
        line = getattr(node, "line", 0) or line
        yield node, label, line
        for k, v in node.items():
            if k == "rollbackSteps" and not include_rollback:
                continue
            if k == "templateRef":
                continue  # a reference is judged by identity, not by whether its name resembles a keyword
            lab = str(v.get("identifier") or v.get("name") or label) if (k in _LABELED and isinstance(v, dict)) else label
            yield from walk(v, lab, line, include_rollback)
    elif isinstance(node, list):
        for item in node:
            yield from walk(item, label, line, include_rollback)


def flatten_stages(items) -> list[dict]:
    """Pipeline `stages:` -> ordered list of stage mappings (parallel groups are flattened)."""
    out: list[dict] = []
    for it in items or []:
        if not isinstance(it, dict):
            continue
        if isinstance(it.get("stage"), dict):
            out.append(it["stage"])
        elif isinstance(it.get("parallel"), list):
            out.extend(flatten_stages(it["parallel"]))
    return out


# ---------------------------------------------------------------------------
# Evidence
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Evidence:
    cap: str
    kind: str  # template | lookalike | inline | weak
    detail: str
    version_status: str | None = None
    step: str | None = None
    via: tuple = ()
    line: int = 0

    def text(self) -> str:
        s = f"{self.kind}:{self.detail}"
        if self.via:
            s += " via " + " > ".join(self.via)
        if self.step:
            s += f" [{self.step}]"
        return s


@dataclass
class Collected:
    evidence: list = field(default_factory=list)
    unresolved: list = field(default_factory=list)  # template refs we could not open
    refs: list = field(default_factory=list)  # (Ref, line, via) via = wrapper chain, () when direct


@dataclass
class Check:
    """One row of the report: a deploy stage judged against one capability."""
    repo: str
    team: str | None
    pipeline: str
    pipeline_id: str
    stage_id: str
    stage_name: str
    line: int
    deploy_kind: str  # native | template | heuristic | unknown
    deploy_detail: str | None
    capability: str
    required: bool
    status: str
    reasons: list
    flags: list
    evidence: list


@dataclass
class Use:
    """A template reference found at pipeline level (feeds the template inventory)."""
    repo: str
    pipeline: str
    stage_id: str
    scope: str
    ident: str
    version: str | None
    line: int
    via: str = ""  # "" = referenced directly by the pipeline; otherwise the wrapper chain


@dataclass
class RepoResult:
    repo: str
    team: str | None = None
    branch: str | None = None
    commit: str | None = None
    has_harness: bool = False
    fetch_error: str | None = None
    local_templates: int = 0
    pipelines: list = field(default_factory=list)
    checks: list = field(default_factory=list)
    uses: list = field(default_factory=list)
    parse_errors: list = field(default_factory=list)


@dataclass
class _Unit:
    stage_id: str
    name: str
    line: int
    kind: str | None
    detail: str | None
    col: Collected


# ---------------------------------------------------------------------------
# The analyzer
# ---------------------------------------------------------------------------

class Analyzer:
    def __init__(self, rules: Rules, golden: Index, local: Index | None = None):
        self.rules, self.golden, self.local = rules, golden, local
        self._memo: dict[tuple, Collected] = {}

    # -- resolution ---------------------------------------------------------
    def resolve(self, ref: Ref) -> Tmpl | None:
        for idx in (self.local, self.golden):  # a repo's own template shadows the golden one
            if idx is not None:
                t = idx.get(ref.scope, ref.ident, ref.version)
                if t is not None:
                    return t
        return None

    # -- evidence -----------------------------------------------------------
    def collect(self, node, label=None, stack=()) -> Collected:
        out, seen = Collected(), set()
        for n, lab, line in walk(node, label, 0, self.rules.policy.count_rollback_steps):
            if isinstance(n, str):
                self._fingerprints(n, lab, line, out, seen)
            elif "templateRef" in n:
                self._reference(n, lab, line, out, stack)
        return out

    def _fingerprints(self, text, label, line, out, seen):
        for cap in self.rules.capabilities.values():
            for kind, group in (("inline", cap.strong), ("weak", cap.weak)):
                for name, rx in group:
                    key = (cap.key, kind, name, label)
                    if key not in seen and rx.search(text):
                        seen.add(key)
                        out.evidence.append(Evidence(cap.key, kind, name, None, label, (), line))

    def _reference(self, d, label, line, out, stack):
        ref = parse_ref(d)
        out.refs.append((ref, line, ()))
        canonical = False
        for cap in self.rules.capabilities.values():
            for rule in cap.templates:
                if rule.ident != ref.ident:
                    continue
                if ref.scope in rule.scopes:
                    canonical = True
                    out.evidence.append(Evidence(cap.key, "template", ref.shown, rule.version_status(ref.version), label, (), line))
                else:
                    out.evidence.append(Evidence(cap.key, "lookalike", ref.shown, None, label, (), line))
        if canonical:
            return  # identity is the proof; no need to look inside the platform's own template
        t = self.resolve(ref)
        if t is None:
            out.unresolved.append(ref.shown)
            return
        key = (t.scope, t.ident, t.version)
        if key in stack:
            return  # template cycle
        inner = self._expand(t, stack + (key,))
        via = f"{ref.raw}@{t.version or 'stable'}"
        out.evidence.extend(replace(e, via=(via,) + e.via, step=label, line=line) for e in inner.evidence)
        out.unresolved.extend(inner.unresolved)
        out.refs.extend((r, line, (via,) + v) for r, _, v in inner.refs)

    def _expand(self, t: Tmpl, stack) -> Collected:
        key = (t.scope, t.ident, t.version)
        if key not in self._memo:
            self._memo[key] = self.collect(t.spec, None, stack) if t.spec is not None else Collected()
        return self._memo[key]

    # -- what is a deploy? ----------------------------------------------------
    def deploy_kind(self, stage: dict, stack=()):
        """-> (kind, detail). kind: native | template | heuristic | unknown | None (not a deploy)."""
        pol = self.rules.policy
        if stage.get("type") in pol.deploy_stage_types:
            spec = stage.get("spec")
            return "native", (spec.get("deploymentType") if isinstance(spec, dict) else None)
        tmpl = stage.get("template")
        if isinstance(tmpl, dict) and "templateRef" in tmpl:
            ref = parse_ref(tmpl)
            t = self.resolve(ref)
            if t is None:
                return "unknown", f"stage template {ref.shown} is not in the golden repo or this repo"
            key = (t.scope, t.ident, t.version)
            if key not in stack and isinstance(t.spec, dict):
                kind, detail = self.deploy_kind(t.spec, stack + (key,))
                if kind:
                    return ("heuristic" if kind == "heuristic" else "template"), detail
            return None, None
        if pol.deploy_commands:
            for n, _, _ in walk(stage, None, 0, pol.count_rollback_steps):
                if isinstance(n, str):
                    for rx in pol.deploy_commands:
                        m = rx.search(n)
                        if m:
                            return "heuristic", " ".join(m.group(0).split())
        return None, None

    def _pipeline_template_kind(self, tmpl: dict):
        ref = parse_ref(tmpl)
        t = self.resolve(ref)
        if t is None:
            return "unknown", f"pipeline template {ref.shown} is not in the golden repo or this repo"
        spec = t.spec if isinstance(t.spec, dict) else {}
        for st in flatten_stages(spec.get("stages")):
            kind, detail = self.deploy_kind(st)
            if kind:
                return "template", detail
        return None, None

    # -- one pipeline -----------------------------------------------------------
    def audit_pipeline(self, doc: Doc, repo: str, team: str | None):
        p = doc.body["pipeline"]
        pid = str(p.get("identifier") or doc.path)
        units: list[_Unit] = []
        for st in flatten_stages(p.get("stages")):
            if _disabled(st):
                continue
            kind, detail = self.deploy_kind(st)
            sid = str(st.get("identifier") or st.get("name") or "?")
            units.append(_Unit(sid, str(st.get("name") or ""), getattr(st, "line", 0), kind, detail, self.collect(st, sid)))
        ptmpl = p.get("template")
        if not units and isinstance(ptmpl, dict) and "templateRef" in ptmpl:
            kind, detail = self._pipeline_template_kind(ptmpl)
            units.append(_Unit("(pipeline template)", str(ptmpl.get("templateRef")), getattr(ptmpl, "line", 0), kind, detail, self.collect(ptmpl)))

        mode, n = self.rules.policy.coverage, len(units)
        checks: list[Check] = []
        for i, u in enumerate(units):
            if not u.kind:
                continue
            span = {"same_stage": [i], "same_or_later_stage": range(i, n), "pipeline": range(n)}[mode]
            evidence = [e for j in span for e in units[j].col.evidence]
            unresolved = [x for j in span for x in units[j].col.unresolved]
            for cap in self.rules.capabilities.values():
                status, reasons, flags = decide(cap, evidence, unresolved)
                checks.append(Check(
                    repo, team, doc.path, pid, u.stage_id, u.name, u.line, u.kind, u.detail,
                    cap.key, cap.required, status, reasons, flags,
                    sorted({e.text() for e in evidence if e.cap == cap.key}),
                ))
        uses = [Use(repo, doc.path, u.stage_id, r.scope, r.ident, r.version, line, " > ".join(v))
                for u in units for r, line, v in u.col.refs]
        info = {"path": doc.path, "id": pid, "name": str(p.get("name") or ""), "stages": n,
                "deploy_stages": sum(1 for u in units if u.kind and u.kind != "unknown"),
                "unclassified_stages": sum(1 for u in units if u.kind == "unknown")}
        return info, checks, uses


def decide(cap: Capability, evidence: list, unresolved: list):
    """Reduce evidence to (status, reasons, flags) for one capability. Order encodes precedence."""
    mine = [e for e in evidence if e.cap == cap.key]

    def pick(kind, *statuses):
        return [e for e in mine if e.kind == kind and (not statuses or e.version_status in statuses)]

    good = pick("template", "current", "unpinned")
    if good:
        flags = []
        if not pick("template", "current"):
            flags.append("unpinned_version")
        if all(e.via for e in good):
            flags.append("via_wrapper")
        return "compliant", [], flags
    for status, label, kinds in (
        ("outdated", "deprecated template version", ("deprecated",)),
        ("review", "template version not classified in capabilities.yaml", ("unlisted",)),
        ("blocked", "blocked template version", ("blocked",)),
    ):
        hit = pick("template", *kinds)
        if hit:
            return status, [f"{label}: {hit[0].detail}"], (["via_wrapper"] if hit[0].via else [])
    inline = pick("inline")
    if inline:
        wanted = ", ".join(r.ident for r in cap.templates) or "the platform template"
        return "inline", [f"hand-rolled implementation ({', '.join(sorted({e.detail for e in inline}))}); use {wanted}"], []
    look = pick("lookalike")
    if look:
        allowed = "/".join(sorted({s for r in cap.templates for s in r.scopes}))
        return "lookalike", [f"{look[0].detail}: platform template identifier at the wrong scope (expected {allowed}); likely a forked copy"], []
    reasons = []
    if unresolved:
        reasons.append("could not inspect: " + ", ".join(sorted(set(unresolved))))
    weak = pick("weak")
    if weak:
        reasons.append("weak hint only (" + ", ".join(sorted({e.detail for e in weak})) + ")")
    if reasons:
        return "review", reasons, []
    return "missing", ["no deployment evidence in the covered steps"], []


# ---------------------------------------------------------------------------
# Repo / snapshot level
# ---------------------------------------------------------------------------

def analyze_repo(rules: Rules, golden: Index, repo_dir: Path) -> RepoResult:
    meta: dict = {}
    mp = repo_dir / "_meta.json"
    if mp.is_file():
        try:
            meta = json.loads(mp.read_text(encoding="utf-8"))
        except ValueError:
            meta = {}
    res = RepoResult(
        repo=meta.get("repo") or repo_dir.name, team=meta.get("team"), branch=meta.get("branch"),
        commit=meta.get("commit"), fetch_error=meta.get("error"),
    )
    harness = repo_dir / ".harness"
    res.has_harness = harness.is_dir()
    if res.fetch_error or not res.has_harness:
        return res
    docs, res.parse_errors = load_docs(repo_dir, harness)
    local = index_templates(docs)
    res.local_templates = len(local)
    az = Analyzer(rules, golden, local)
    for d in docs:
        if d.kind == "pipeline":
            info, checks, uses = az.audit_pipeline(d, res.repo, res.team)
            res.pipelines.append(info)
            res.checks.extend(checks)
            res.uses.extend(uses)
    return res


def scan_snapshot(rules: Rules, golden: Index, snapshot: Path) -> list[RepoResult]:
    return [analyze_repo(rules, golden, d) for d in sorted(p for p in Path(snapshot).iterdir() if p.is_dir())]


def repo_status(r: RepoResult, cap_key: str) -> str:
    if r.fetch_error:
        return "fetch_error"
    if not r.has_harness:
        return "no_harness"
    statuses = [c.status for c in r.checks if c.capability == cap_key]
    if statuses:
        return max(statuses, key=SEVERITY.get)
    return "parse_error" if r.parse_errors else "no_deploy"


# ---------------------------------------------------------------------------
# Golden catalog
# ---------------------------------------------------------------------------

_URL = re.compile(r"https?://[^\s\"'\\)>]+")
_MUTATION = re.compile(r"\bmutation\b[^{]*\{\s*(\w+)")
_ENVVAR = re.compile(r"\$\{?([A-Z][A-Z0-9_]{4,})\}?")


def suggest_keywords(spec) -> dict:
    """What a template talks to: endpoints, GraphQL mutations, env vars. For a human to curate into rules."""
    text = "\n".join(n for n, _, _ in walk(spec, None, 0, True) if isinstance(n, str))
    pick = lambda rx: sorted({m.rstrip(".,;") for m in rx.findall(text)})[:6]
    return {"urls": pick(_URL), "graphql_mutations": pick(_MUTATION), "env_vars": pick(_ENVVAR)}


def build_catalog(rules: Rules, golden: Index) -> dict:
    az = Analyzer(rules, golden)
    rows = []
    for t in sorted(golden, key=lambda t: (t.scope, t.ident, _vkey(t.version))):
        col = az.collect(t.spec) if t.spec is not None else Collected()
        canonical = sorted(
            c.key for c in rules.capabilities.values() for r in c.templates if r.ident == t.ident and t.scope in r.scopes
        )
        rows.append({
            "scope": t.scope, "ident": t.ident, "version": t.version, "type": t.kind, "name": t.name,
            "description": t.description, "path": t.path, "line": t.line,
            "canonical_for": canonical,
            "implements": sorted({e.cap for e in col.evidence}),
            "evidence": sorted({e.text() for e in col.evidence}),
            "uses_templates": sorted({r.shown for r, _, v in col.refs if not v}),
            "unresolved": sorted(set(col.unresolved)),
            "suggested_keywords": suggest_keywords(t.spec),
        })
    drift = []
    for cap in rules.capabilities.values():
        for rule in cap.templates:
            found = [t for t in golden if t.ident == rule.ident and t.scope in rule.scopes]
            if not found:
                drift.append(f"{cap.key}: canonical template '{rule.ident}' (scopes: {sorted(rule.scopes)}) is not in the golden repo")
                continue
            have = {t.version for t in found}
            classified = rule.current | rule.deprecated | rule.blocked
            for v in sorted(rule.current - have):
                drift.append(f"{cap.key}: rules list {rule.ident}@{v} as current but the golden repo has no such version")
            if classified:
                for v in sorted(have - classified, key=_vkey):
                    drift.append(f"{cap.key}: {rule.ident}@{v} exists in the golden repo but is not classified in capabilities.yaml")
    return {"templates": rows, "drift": drift}
