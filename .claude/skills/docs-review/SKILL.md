---
name: docs-review
description: Read-only review of the docs/ knowledge base for factual accuracy against the code, currency (stale content), structure (information in the right document and section, TOCs that match the body and agree across the generator docs) and style (no agent instructions, no historical framing outside design_decisions.md). Produces a findings report and changes nothing. Use whenever the user asks to review, audit, check, proofread or sanity-check documentation, a doc file, a generator doc, or a docs diff/branch — even if they only say "look over the docs" or "is genie.md still right?". For writing or updating docs, use docs-maintenance instead.
---

# Reviewing the knowledge base

`docs/` is a knowledge base read by humans and agents alike. A review finds
where it is wrong, out of date, misplaced, or badly written, and **reports**
it. It does not fix anything.

## The review changes nothing

Do not edit, create, move or delete any file during a review — not docs, not
code, not TODOS.md. Flag problems only. The reason: a review is the input to
a decision the user makes; silent fixes mix the reviewer's judgement into the
docs unseen, and a wrong "fix" of a fact is worse than the original error
because nobody re-checks it. If the user wants fixes after reading the report,
that is a separate task (under `docs-maintenance`).

Running read-only commands to check a claim (grep, reading source, `--help`,
`bash -n`, a quick Python import) is fine and encouraged. Running generators,
containers or Slurm jobs is not part of a review — mark such claims
*unverified* instead.

## Scope

- **Named files or a diff/branch**: review those. For a diff, also check the
  docs that the changed code is described in (see the routing table in the
  `docs-maintenance` skill) — a code change with no doc change is a currency
  finding.
- **"The docs" / whole knowledge base**: all of `docs/`. It is ~6.5k lines;
  delegate the per-document accuracy pass to one subagent per large document
  (each told: read-only, report findings in the format below), and do the
  cross-document checks (structure, TOC consistency, duplication) yourself,
  since they need all documents in view.

Start every review by running the mechanical checks:

```bash
python3 .claude/skills/docs-review/scripts/check_structure.py [files...]
```

It reports missing TOCs (documents over 200 lines), TOC entries that match no
heading, H2s missing from the TOC, broken relative links and anchors, and the
H2 sequence of each generator doc side by side. Its output is evidence for
findings, not the review itself — confirm each before reporting it.

## What to check

### 1. Factual accuracy

Every checkable claim should match the code and the shipped files. Prioritize
claims a reader would act on:

- file paths, module/function/class names, CLI commands and flags, config keys
  and their defaults, environment variables, column names, units;
- numbers tied to code (bin counts, cuts, pinned versions, digests, defaults);
- descriptions of behaviour ("the merge averages weights", "falls through to
  `other`") — read the code path, don't trust the sentence;
- cross-document consistency: the same fact stated in two places must agree
  (e.g. `physics.md` summary tables vs. the generator docs; `docs/README.md`
  row descriptions vs. the documents they describe).

Measured numbers and generator-internal behaviour often can't be re-derived
in a review. Check that they carry their conditions and evidence (version,
target, flux, event count; source file; `literature/` citation with arXiv id,
per the evidence bar in `docs-maintenance`). A claim with no stated evidence
is a finding even if it might be true.

### 2. Currency

Content that described the code at some point but no longer does:

- references to files, functions, flags, config keys, branches or images that
  no longer exist (grep for them);
- "currently", "for now", "not yet", "planned", "TODO", "will be" — each is a
  claim about the present that may have expired; check it;
- statements contradicted by recent commits (`git log -- <path>` on the code
  the section describes is a fast way to spot drift);
- items in `.claude/TODOS.md` that the docs describe as done, or vice versa.

### 3. Structure

**Between documents.** `docs/README.md` defines what each document owns.
Flag information that lives in the wrong document (e.g. generator-specific
implementation detail in `physics.md` beyond its summary-table role,
operational runbook steps in a generator doc), facts stated in full in two
places instead of once plus a link, and documents missing from the README
table.

**Within documents.** Each section's content should match its heading; flag
content sitting under the wrong heading (a validation result under
Implementation, a user-facing limitation buried in Design notes).

**Generator docs** (`docs/generators/*.md`) follow the shared template in
`docs/adding_generators.md` ("Documenting the generator"): same H2s, same
order, so a reader finds the same thing in the same place in every generator
doc. Flag H2s out of order, renamed relative to the template or to the other
generator docs (e.g. "Reweight universes and variations" vs. "Reweight
universes"), or extra H2s that should be H3s inside a template section.
Absent optional sections are fine; reordering is not.

**TOCs.** A document over ~200 lines should open with a TOC (a list of links
to its own H2 anchors, before the first H2). The TOC must match the body: same
headings, same order, every anchor resolves. The generator docs' TOCs must be
consistent with each other, which follows if each matches its body and the
bodies follow the template.

### 4. Style

- **No agent instructions.** Docs describe; they don't direct agents.
  "Agents should…", "Claude must…", "do not reorder these branches", "always
  update X when…" are findings — the rule belongs in `CLAUDE.md`, `.claude/`
  or a skill, and the doc should state the underlying fact ("the branch order
  is load-bearing because…"). Procedures a reader performs on purpose — setup
  steps, a cluster runbook, a command to run — are not agent instructions.
- **No historical framing outside `docs/design_decisions.md`.** Docs say what
  is, not what was. Flag "previously", "used to", "was changed to", "no
  longer", "now (since …)", "we switched from", "originally", and narratives of
  how a bug was found. A dated heading in a generator doc's *Design notes*
  (`### Foo (2026-07)`) records when a decision was made and is fine; the text
  under it should still give the rationale, not a story of the change.
- Plain, precise prose; units stated; code identifiers in backticks; relative
  links; no filler, no marketing tone; terminology consistent with
  `physics.md` (e.g. `xsec_weight`, "per nucleon", job vs. generator version).

## Report format

Report in the chat unless the user asks for a file. Order findings by how
much they would mislead a reader: wrong facts first, style last.

```markdown
# Docs review: <scope>

<one-paragraph summary: what was reviewed, how many findings per category,
the most important one>

## Findings

### <file>
- **[accuracy|currency|structure|toc|style] <severity: high|medium|low>** —
  `<file>:<line>` — <what is wrong, one sentence>.
  Evidence: <what you checked: code location, grep result, command output>.
  Suggested direction: <what the fix would be, not the rewritten text>.

## Cross-document findings
<duplication, misplaced content, generator-doc template/TOC drift>

## Not verified
<claims you could not check in a read-only review and why — e.g. measured
runtimes, generator-internal behaviour needing a run>
```

Severity: **high** — a reader acting on it gets a wrong result or a failing
command; **medium** — misleading, stale or misplaced but not directly harmful;
**low** — style and polish. Quote the offending text briefly when it helps
locate it. Don't pad: no findings in a category means saying so in the
summary, not inventing marginal ones.
