Harness Template Compliance Audit: Write-up
Nov 5, 2026 · @Orin Bishop
Summary
The audit shows, for every repo in the program, whether its Harness deploy pipelines use the platform team's New Relic deployment marker template, and it publishes the result to a Confluence page. It is built and passes 53 tests on generated and hand-written sample pipelines. It has not been run against your real repos, GitHub, Harness or Confluence.
It answers three questions:
• Which repos deploy through Harness, as opposed to only building an image?
• In each deploy stage, is the required template used? If not, is it missing, hand-rolled, a forked copy, outdated or blocked?
• Which golden templates are used, by whom, and which are unused?
Three things must be filled in before the first run: the real template identifier and version labels in capabilities.yaml, the Confluence template reference and inputs in the pipeline, and the connectors, image and secrets marked REPLACE_ME. The setup checklist below lists every one.
Why a rebuild
The July script searched each pipeline for keywords within five lines of templateRef: and wrote one HTML page per team. It could not tell a template reference from a mention, could not see inside wrapper templates, and had no way to say "I could not tell". This tool reads the YAML as structure and follows every templateRef into the golden repo.
Situation
Keyword grep (July)
This tool
Pipeline reaches the marker only through a golden stage template
Missed: the keyword is not in the pipeline
Compliant, flagged via_wrapper
Marker step exists only under rollbackSteps
Counted as adopted
Not counted: missing
Marker step disabled with when: condition: false
Counted as adopted
Not counted: missing
Team pasted the NerdGraph curl inline
"Candidate" if no templateRef is nearby
inline, naming the fingerprint matched
Template is not in the golden repo
Not reported
review: could not inspect
Output
One HTML page per team
One Confluence page plus five flat files
How it works
The tool downloads the golden templates and every team's .harness folder, judges every deploy stage against the rules, and writes one page for Confluence. The steps share nothing but folders, so any of them can be run or replaced on its own.
Each arrow from a repo to its folder is fetch, which downloads only the YAML under .harness. catalog is a side check: it compares capabilities.yaml with golden/, prints any drift, and writes no report. scan writes report/, and both the Confluence page and the exit-code policy read from it.
How a deploy stage is judged
Each stage goes through three questions: is it a deploy, what evidence exists, and which status that evidence adds up to.
1. Is it a deploy?
Kind
Meaning
native
The stage type is Deployment.
template
A stage template that resolves to a Deployment stage.
heuristic
A CI or Custom stage running helm upgrade, kubectl apply, terraform apply and similar. Listed separately for a human to confirm.
unknown
A stage template found in neither the golden repo nor the repo. Counted separately, not as a deploy.
2. What evidence exists? Evidence is collected from the stage and, recursively, from every template it references: the golden repo plus the repo's own templates, with cycle protection. There are four kinds: a reference to a canonical template (direct or through a wrapper), the canonical identifier at the wrong scope (a fork), a hand-rolled implementation matched by a strong fingerprint, and a weak hint.
3. Which status? The first matching row wins.
Status
Counts as
Rule
compliant
Pass
Canonical template, current or unpinned version.
outdated
Pass
Canonical template, deprecated version.
review
Human
Canonical template at a version the rules never classified; a template that could not be opened; or a weak hint only.
blocked
Fail
Canonical template at a blocked version.
inline
Fail
Hand-rolled implementation, no template.
lookalike
Fail
Canonical identifier at the wrong scope, likely a forked copy.
missing
Fail
A deploy with no evidence at all.
Deliberate rules
• Identity beats content: a reference to the canonical template is proof, so the tool does not open it.
• Wrapper templates count: a golden stage template that calls the marker makes every pipeline using it compliant.
• Unknown is not missing: a template the tool cannot open produces review, never missing.
• Marker steps under rollbackSteps, and steps with a literal-false when condition, do not count.
• Template names are not evidence: templateRef values are never matched against keywords.
• Coverage defaults to same_or_later_stage: a marker in the deploy stage or any later stage covers it; a marker in an earlier stage does not. same_stage and pipeline are the alternatives, set in capabilities.yaml.
What is in the package
Transfer the whole harness-audit folder; the zip holds all of it. It needs Python 3.9 or later and one dependency, PyYAML.
File
Job
README.md
Usage, design rationale, known limits, and a nine-question checklist for comparing with another implementation.
capabilities.yaml
The rules, and the only file the platform team edits: which template is required, which versions are current, deprecated or blocked, and how to recognize a hand-rolled copy.
harness_audit/__main__.py
The command line: fetch, catalog, scan.
harness_audit/analyze.py
The core: rules, template index, evidence collection, deploy detection, status decisions.
harness_audit/fetch.py
Downloads only the .harness folder of each repo through the GitHub REST API, standard library only.
harness_audit/report.py
Writes the Confluence page and the five other outputs from one shared description of the report.
harness_audit/yamlio.py
YAML loader that keeps line numbers and finds pipelines and templates by content.
harness/template_audit_pipeline.yaml
The Harness pipeline: fetch, scan, publish to Confluence, then apply the exit-code policy.
tests/test_audit.py
53 tests.
fetch and scan meet at a plain directory, so either can be replaced, for example by an existing downloader:
<snapshot>/<owner>__<repo>/_meta.json     {"team": ..., "repo": "owner/name", "commit": ..., "error": null}
<snapshot>/<owner>__<repo>/.harness/**.yaml
A repo with no .harness folder, or one that failed to download, still gets a _meta.json, so it appears in the report instead of vanishing.
Setup checklist
Every value below is a placeholder today. Search both capabilities.yaml and the pipeline for REPLACE to find them.
[ ] Install PyYAML from the internal package index.
[ ] Create a read-only corporate GitHub token with access to every repo in teams.json. For GitHub Enterprise Server, set GITHUB_API_URL to https://HOST/api/v3.
[ ] Write golden.json ([{"team": "platform", "repo": "owner/golden-templates"}]) and teams.json ([{"team": "Payments", "repo": "owner/payments-service"}, ...]).
[ ] Run python -m harness_audit fetch for the golden repo, then python -m harness_audit catalog. Read the list of templates that implement the marker but are not canonical.
[ ] In capabilities.yaml, replace the placeholder identifier NR_Deployment_Marker and the placeholder version labels (v2.0.0 current; v1.0.0 and v1.1.0 deprecated) with the real ones.
[ ] Compare the strong fingerprints in capabilities.yaml with the golden template's body. catalog prints suggested keywords taken from that body.
[ ] In the pipeline, replace projectIdentifier, orgIdentifier, the GitHub, Kubernetes and registry connectors, the namespace, and the GitHub token secret name.
[ ] Open the Confluence reporting template and paste its real templateRef, versionLabel and input block into the "Publish to Confluence" step. Today it guesses two environment variables, CONFLUENCE_PAGE_ID and REPORT_HTML.
[ ] Set the pipeline variables that are asked at run time: GOLDEN_REPO, CONFLUENCE_PAGE_ID, PYTHON_IMAGE (approved internal image with Python 3.9+ and bash), PIP_INDEX_URL, GITHUB_API_URL.
[ ] Run once by hand with FAIL_ON=never, read the Confluence page, and fix anything the rules got wrong.
[ ] Add the Cron trigger (the README has the YAML, for example Mondays 06:00).
[ ] Decide whether FAIL_ON should ever be anything other than never. The default makes this an inventory, not a gate.
Running it and reading the output
The pipeline runs the same four commands you can run by hand, then publishes report/report.html and applies the exit-code policy:
python -m harness_audit fetch   --repos golden.json --out golden --path .harness
python -m harness_audit catalog --golden golden --rules capabilities.yaml
python -m harness_audit fetch   --repos teams.json  --out snapshot
python -m harness_audit scan    --snapshot snapshot --golden golden --rules capabilities.yaml --out report
The last steps run in a fixed order: scan, publish to Confluence, then apply policy. A failing FAIL_ON policy therefore still updates the page and only then fails the run. A configuration error (exit code 3) stops before publishing, because there is nothing correct to publish. FAIL_ON defaults to never.
File
One row per
Use
report.html
run
The Confluence page: the summary, the full repo table, and the golden-template usage table, with colored status badges.
summary.md
run
The same summary as Markdown; points to the CSVs instead of repeating the full tables.
repos.csv
repo
The inventory, with one rolled-up status per required capability.
stages.csv
deploy stage and capability
The evidence, with file and line, reasons, and flags such as via_wrapper and unpinned_version.
templates.csv
template version
Repos using it directly, repos using it only through a wrapper, unused templates, and repos hand-rolling it instead.
findings.json
everything
Lossless copy for other tools.
report.html is an HTML fragment, not a document: no <html> or <body>, no <style>, <script> or <details>, and only characters valid in XML. That is what Confluence storage format accepts. The page is overwritten on each run, so the page history in Confluence is the run history. If the template wants a complete HTML document, wrap the file in <html><body> and </body></html>.
Adding another required template
A second template is one new block under capabilities: in capabilities.yaml, with no code change. Every output then gains a section for it, a status:<name> column in repos.csv, and rows in stages.csv and templates.csv. Set required: false to inventory adoption without judging it.
capabilities:
  my_next_template:
    description: What every deploy must do, in one sentence.
    required: true
    templates:
      - ident: Platform_Template_Identifier
        scopes: [account]
        versions: {current: ["v1"], deprecated: [], blocked: []}
    strong:   # text that IS the implementation; a hand-rolled copy is reported as inline
      - {name: its-api-call, regex: 'something-specific-to-the-implementation'}
    weak:     # suggestive only; produces review
      - {name: its-wording, regex: 'looser hint'}
catalog prints suggested keywords (endpoints, GraphQL mutations, environment variables) taken from the golden template's own body, so the fingerprints come from the template, not from memory. A requirement needs a canonical template to be satisfiable: with fingerprints alone, a match can only ever be reported as inline and no match as missing.
What was verified, and what was not
Everything below was tested on generated and hand-written sample data. Nothing has touched your real systems.
Verified
• 53 automated tests pass. They cover every status, wrapper templates, rollback and disabled steps, the coverage policy, parallel stages, pipeline templates, repo-local templates, broken YAML, the fetch logic against a fake GitHub client, the command-line exit codes, and the Confluence HTML.
• Four core rules were broken on purpose, and each break made a test fail: counting rollback steps, ignoring unresolved templates, accepting the canonical identifier at any scope, and skipping HTML escaping.
• A full run over five sample repos written in realistic Harness YAML, with runtime inputs, step groups, parallel stages and block-scalar scripts.
• report.html parses as XML, contains none of the elements Confluence strips, and stays well-formed when team and repo names contain <script>, & and control characters.
• The pipeline YAML parses and its embedded script passes bash -n. The publish-then-gate ordering was simulated against the real command line with FAIL_ON=never and FAIL_ON=fail.
• Four defects were found and fixed along the way: a colon in the pipeline description that made the YAML invalid, template usage through wrappers counted as zero, template names tripping the keyword hints, and a noisy drift warning for deleted deprecated versions.
Not verified
• Any live GitHub call. fetch has only run against a fake client, so authentication, rate limiting and the GitHub Enterprise Server URL are untested.
• Importing the pipeline into Harness, including the CI stage and infrastructure fields.
• The Confluence template: its input names are guesses, publishing has not run, and Confluence may drop the colored badges on some versions (the status word stays).
• Your real golden templates and team pipelines, including how the real marker template is identified and versioned.
• Whether treating an unversioned reference as the highest version label matches what Harness treats as stable.
Known limits
These are properties of the design, not defects. The open items that block a first run are in the setup checklist.
• Only <repo>/.harness/ is read. A pipeline stored anywhere else is invisible to the audit.
• A template reference with no versionLabel means "always use the stable version". The stable flag is not in the YAML, so the highest version label in the golden repo stands in for it.
• Parallel stages are ordered as listed, so a marker in a sibling parallel stage does not count as a later stage.
• Chained pipelines (stages of type Pipeline) are not followed. The child pipeline is audited in its own repo.
• Only a literal false in when.condition marks a step as disabled. A condition that evaluates to false at run time is not seen.
• A pipeline that is itself a template is judged as one unit, not stage by stage.
• Templates that exist only in the Harness UI, and not in git, cannot be opened, so they produce review rather than missing.
• Deploys detected by command text (heuristic) can be wrong in both directions. They are listed separately for that reason.
• An unquoted numeric versionLabel such as 1.10 is read by YAML as 1.1.
• Each run is a snapshot of the default branch (or the branch named in teams.json) at fetch time. The commit is recorded in repos.csv, but there is no history across runs other than the Confluence page versions.
Security and organization notes
The tool is read-only against GitHub and writes nothing outside its own output folders.
• Accounts. Use the corporate GitHub account and token only. The token is read from the GH_TOKEN environment variable (in the pipeline, from a Harness secret) and is never written to disk, to _meta.json or to a report.
• Network. fetch sends only GET requests, and only to the GitHub API address you configure. No other service is contacted.
• Packages and images. Install PyYAML from the internal package index and run the pipeline on an approved internal image. Nothing in the pipeline pulls from a public registry.
• Files. fetch refuses to write any path that would land outside the snapshot folder.
• Reports. Every report carries an internal-use banner for Via Logic LLC. Restrict the Confluence page to internal users, and do not publish the output or send it to a third-party service.
• Findings are sensitive. The report lists which teams deploy without the required template. Treat it like any internal audit result.