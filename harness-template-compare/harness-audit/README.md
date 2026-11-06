# harness-audit

**INTERNAL USE ONLY - Via Logic LLC.**

Answers three questions across every repo in the program:

1. Which repos actually **deploy** through Harness (not just build an image)?
2. In each deploy stage, is the **required platform template** used (today: the New Relic deployment marker)? If not, is it missing, hand-rolled, a forked copy, outdated, or blocked?
3. Which golden templates are **used, by whom, and which are unused**? Who is hand-rolling something a template already does?

Python 3.9+, one dependency (PyYAML, from the internal index). No other services.

## Quick start

```bash
pip install pyyaml                       # from the internal package index
export GH_TOKEN=...                      # corporate GitHub token, read-only
export GITHUB_API_URL=...                # only for GitHub Enterprise Server: https://HOST/api/v3

# 1. golden templates   (golden.json = [{"team": "platform", "repo": "owner/golden-templates"}])
python -m harness_audit fetch   --repos golden.json --out golden --path .harness

# 2. FIRST TIME ONLY: pin the canonical template and its versions
python -m harness_audit catalog --golden golden --rules capabilities.yaml
#    read "implements a capability but is not canonical", then edit capabilities.yaml

# 3. every team repo    (teams.json = [{"team": "Payments", "repo": "owner/payments-service"}, ...])
python -m harness_audit fetch   --repos teams.json --out snapshot

# 4. audit
python -m harness_audit scan    --snapshot snapshot --golden golden --rules capabilities.yaml --out report

python -m unittest discover -s tests     # 53 tests

# open report/report.html in a browser, or publish it to Confluence (below)
```

`harness/template_audit_pipeline.yaml` runs steps 1-4, publishes the report to Confluence, then applies the exit-code policy. Add a Cron trigger (for example Mondays 06:00):

```yaml
trigger:
  name: Weekly audit
  identifier: Weekly_audit
  enabled: true
  orgIdentifier: REPLACE_ME
  projectIdentifier: REPLACE_ME
  pipelineIdentifier: Template_Compliance_Audit
  source:
    type: Scheduled
    spec:
      type: Cron
      spec:
        expression: 0 6 * * 1
```

## Layout

```
capabilities.yaml          the rules: what is required, which templates satisfy it, how to spot a hand-rolled copy
harness_audit/
  yamlio.py                YAML loader that keeps line numbers; finds pipelines and templates by content
  analyze.py               rules, template index, evidence, deploy detection, status decisions
  report.py                report.html (Confluence), summary.md, repos.csv, stages.csv, templates.csv, findings.json
  fetch.py                 GitHub REST download of only the .harness folder (stdlib only)
  __main__.py              CLI: fetch | catalog | scan
harness/template_audit_pipeline.yaml
tests/test_audit.py
```

**The contract between `fetch` and `scan`** is a directory, so either half can be replaced (for example by an existing downloader):

```
<snapshot>/<owner>__<repo>/_meta.json        {"team": ..., "repo": "owner/name", "commit": ..., "error": null}
<snapshot>/<owner>__<repo>/.harness/**.yaml
```

A repo with no `.harness`, or one that failed to download, still gets a `_meta.json`, so it appears in the report instead of vanishing.

## How a deploy stage is judged

**1. Is it a deploy?** (`deploy_kind`)

| kind | meaning |
|---|---|
| `native` | stage `type` is in `deploy_stage_types` (default `Deployment`) |
| `template` | a stage template that resolves to a Deployment stage |
| `heuristic` | a CI/Custom stage running `helm upgrade`, `kubectl apply`, `terraform apply`, etc. Listed separately for a human to confirm |
| `unknown` | a stage template that is in neither the golden repo nor the repo, so we cannot tell. Counted separately, not as a deploy |

**2. What evidence exists?** Collected from the stage and, recursively, from every template it references (golden repo plus the repo's own `.harness/templates`), with cycle protection:

| evidence | meaning |
|---|---|
| `template` | a `templateRef` to a canonical template, directly or through a wrapper template |
| `lookalike` | the canonical identifier at the wrong scope (a project-level fork of an account-level template) |
| `inline` | a **strong** fingerprint matched: the implementation itself (for example `changeTrackingCreateDeployment`) |
| `weak` | a **weak** fingerprint matched: suggestive only |

**3. Status** (first match wins)

| status | counts as | rule |
|---|---|---|
| `compliant` | pass | canonical template, current or unpinned version |
| `outdated` | pass | canonical template, deprecated version |
| `review` | human | canonical template at a version the rules never classified; or a template we could not open; or a weak hint only |
| `blocked` | fail | canonical template at a blocked version |
| `inline` | fail | hand-rolled implementation, no template |
| `lookalike` | fail | forked copy |
| `missing` | fail | a deploy with no evidence at all |

Deliberate choices:

- **Identity beats content.** A reference to the canonical template is proof; we do not open it.
- **Wrappers count.** A stage template that internally calls the canonical template makes every pipeline using it compliant. Grep-based tools get this wrong.
- **Unknown is not missing.** If the stage references a template we cannot open, the answer is `review`, never `missing`.
- **Rollback and disabled steps do not count.** A marker only in `rollbackSteps`, or a step with `when: condition: false`, is not a deployment marker.
- **Names are not evidence.** `templateRef` values are not fingerprinted, so a template called `...Deployment_Marker...` does not look like a marker by name.
- **Coverage** (`policy.coverage`): `same_or_later_stage` (default), `same_stage`, or `pipeline`. A deploy stage is covered by a marker in itself or a later stage; a marker in an earlier stage does not count.
- **Pipeline templates** (`pipeline: template:`) are treated as one unit.

## Adding another required template

No code change. Add a block to `capabilities.yaml` under `capabilities:` (identifier, scopes, version classes, strong and weak fingerprints). Every report then gains a section, a `status:<name>` column in `repos.csv`, and rows in `stages.csv` and `templates.csv`. Set `required: false` to inventory adoption without judging it.

`catalog` prints suggested keywords (endpoints, GraphQL mutations, env vars) taken from the golden template's own body, so fingerprints come from the template, not from memory.

## Outputs (`--out`, default `report/`)

| file | grain | use |
|---|---|---|
| `report.html` | run | **the Confluence page**: everything in `summary.md` plus the full repo table and the golden-template usage table, with colour-coded status badges |
| `summary.md` | run | read first: counts, action list, review list, parse failures |
| `repos.csv` | repo | the inventory; one rollup status per required capability |
| `stages.csv` | deploy stage x capability | evidence with file and line, reasons, flags (`via_wrapper`, `unpinned_version`) |
| `templates.csv` | template version | `repos_direct`, `repos_via_wrapper`, unused templates, `repos_hand_rolling_instead` |
| `findings.json` | everything | lossless, for other tools |

`--fail-on never|fail|review` sets the exit code (default `never`: it is an inventory, not a gate). Exit 3 means bad rules or fetch configuration.

## Publishing to Confluence

`report/report.html` is the page body. `summary.md` is rendered from the same blocks, so the two cannot disagree; the HTML additionally carries the *All repos* and *Golden template usage* tables, because Confluence is where people browse.

The file is a **fragment**, not a document, and uses only what Confluence storage format accepts: no `<html>`/`<head>`/`<body>`, no `<style>`, `<script>`, `<details>` or `<img>`, no entities that are not valid XML (no `&nbsp;`), every tag closed, all text escaped, XML-illegal control characters removed. Colour is an inline `style` on a `<span>`. The tests parse the output as XML and check for each banned element. If a Confluence version drops the colour, the status word is still in the badge.

The pipeline step *Publish to Confluence* hands `report/report.html` to the platform team's Confluence reporting template. **Its `templateRef`, `versionLabel` and `templateInputs` are placeholders**: the template's real input schema has to be copied in. The page is overwritten on every run, so the page history in Confluence is the run history (the *Generated* line is inside the page).

Order of the last steps: scan, publish, then *Apply policy*. A failing `--fail-on` policy therefore still updates the page, and only afterwards fails the run. A configuration error (exit 3) stops before publishing, since there is nothing correct to publish.

If the template wants a complete HTML document rather than a fragment, wrap the file: `<html><body>` + contents + `</body></html>`.

## Known limits (stated so nobody is surprised)

- Only `<repo>/.harness/` is read. Pipelines stored elsewhere are invisible.
- A reference with no `versionLabel` means "stable"; the stable flag is not in the YAML, so the highest version label in the golden repo stands in for it.
- Parallel stages are ordered as listed. Chained pipelines (`type: Pipeline` stages) are not followed; the child is audited in its own repo.
- Only a literal `false` in `when.condition` marks a step disabled.
- Templates that exist only in the Harness UI (not in git) are `review`, not `missing`.
- An unquoted numeric `versionLabel` such as `1.10` is read by YAML as `1.1`.

## Org rules this tool follows

Use the corporate GitHub account and token only. Packages from the internal index. Reports are internal; do not publish them or send them to third-party services. The tool makes no calls except to the GitHub API you configure.

## For a reviewer comparing this with another implementation

Ask of the other code:

1. Does it parse YAML, or grep text? Grep cannot tell a reference from a mention, or a step from a rollback step.
2. Does it resolve `templateRef` through the golden repo, including templates that wrap templates?
3. Can it say "I could not tell" (`review`) separately from "not there" (`missing`)?
4. Is the knowledge of what each template is and how to recognise it in a data file, or hard-coded?
5. Does it separate download from analysis, so either can be rerun or swapped?
6. Does every finding carry a file and line, and does a broken file surface as a finding instead of crashing the run?
7. Does it report versions: current, deprecated, blocked, and drift between the rules and the golden repo?
8. Are there tests, including for the cases above? Here: 53, and each core rule was checked by breaking it and watching a test fail.
9. How many files does it produce and consume? Here: 5 modules, 1 rules file, 1 pipeline, 1 test file; 6 report files (one of them the Confluence page).
