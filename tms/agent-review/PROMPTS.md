# Example prompts for the user's own agent

Replace `/path/to/osx_tools`, `/path/to/source`, and `/path/to/project` with the
actual paths. The source root is the directory passed to TMS, which may contain
several projects. Provide these prompts to your preferred agent; TMS does not
call an AI service or install an agent runtime.

## 1. Read-only discovery across projects

```text
Read /path/to/osx_tools/tms/agent-review/AGENTS.md and the TMS README it links to.
Audit /path/to/source for opportunities to reduce future backup input.
This is read-only: do not change rules, delete files, repack Git, download data,
change scheduling, or run backups.

Measure the largest directories without double-counting nested data or hard
links. Separate downloaded dependencies/models and build outputs from original
data and authored results. For each candidate, give its size, reproducibility
evidence, exact recovery recipe or missing evidence, and a proposed narrow rule.
Also identify valuable data that existing central exclusions may already hide.
Return an exact configuration proposal and representative --explain checks.
Keep unknown or only conditionally reproducible data included by default.
```

## 2. Draft one project's local overrides

```text
Read /path/to/osx_tools/tms/agent-review/AGENTS.md and the installed TMS docs.
The backup source root is /path/to/source; the project is /path/to/project.
Read the current central policy and applicable ancestor/local .tmsignore files.

Propose a project-local .tmsignore diff that excludes only confirmed reproducible
downloads/build products. Include exceptions for unique data hidden by central
defaults. For example, check whether any locally trained models under .models
need a ! include rule. Do not exclude all local-data or use .gitignore wholesale.

Preserve unrelated rules and project edits. Do not modify anything yet. Show the
evidence, proposed diff, estimated avoided bytes, recovery instructions, and
verification paths using --source /path/to/source. Clearly distinguish checks
of the current policy from checks of a proposed policy.
```

## 3. Review mixed model and meeting data

```text
Use /path/to/osx_tools/tms/agent-review/AGENTS.md to review /path/to/project.
The actual TMS source is /path/to/source. Inspect .models, model caches, build
trees, environments, and the children of local-data using metadata and project
documentation. Do not read recordings or transcript contents to classify size.

Verify model repository IDs, immutable revisions, acquisition manifests and
hashes. Distinguish downloaded weights from fine-tunes/adapters, downloaded test
audio from irreplaceable recordings, and rebuildable outputs from historical
evaluation evidence or manual corrections. Keep unknown items.

Produce a read-only exclusion proposal. Keep source URLs, package lockfiles,
model inventories, review annotations, prompts, and actual results available for
recovery. Do not run inference, fetch models, delete data, or change policy.
```

## 4. Audit an existing policy for accidental omissions

```text
Read /path/to/osx_tools/tms/agent-review/AGENTS.md and the TMS limitations.
Review the central exclusions and local overrides affecting /path/to/source.
Prioritize finding unique data currently excluded, not maximizing saved bytes.

Check model outputs, Terraform state, saved meeting records, edited transcripts,
annotations and experiment results. Use tms --explain with the actual source
root for representative files. Propose narrow ! include corrections with the
winning current rule and its origin. Explain sibling/descendant effects.
Do not change rules or touch backup data; return the proposed patch for review.
```

## 5. Apply an already reviewed, explicitly approved patch

```text
Follow /path/to/osx_tools/tms/agent-review/AGENTS.md and the installed TMS docs.
I authorize applying ONLY the configuration patch pasted below to these named
files: [exact central/local configuration paths]. The actual source root is
/path/to/source. If the files differ from the reviewed baseline, preserve other
edits and stop for clarification where the patch would conflict.

Save the prior configuration privately. Apply the approved changes, then run
--explain for each representative excluded path and each required keep path,
including a kept file inside an excluded parent and an unaffected sibling.
Report the actual winning rule/file and give rollback instructions.

This authorization does not include deletion, Git cleanup, committing/pushing,
schedule changes, downloads, or production backups. Do not request approval
again for the exact configuration edits authorized here.

APPROVED PATCH:
[paste the reviewed diff]
```

## 6. Evaluate a possible central default

```text
Read /path/to/osx_tools/tms/agent-review/AGENTS.md and the current TMS defaults.
Evaluate whether [specific directory pattern] should be a central exclusion
across these source roots: [explicit source roots]. Work read-only.

Find affected projects and valuable counterexamples, verify recovery recipes,
and compare a central rule plus local keep exceptions against project-local
exclusions. Include a before/after decision matrix with the actual source roots.
Do not add a global wildcard based only on names or file size. Return a concrete
proposal with affected paths, evidence, exceptions, estimated benefit, and gaps.
```
