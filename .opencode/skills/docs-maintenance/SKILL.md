---
name: docs-maintenance
description: Rules for maintaining the version-controlled knowledge base in docs/. Use when changing code whose behaviour a docs/ file describes, or when a session establishes new domain knowledge about the generators or the physics conventions.
---

# Maintaining the knowledge base

Use the `docs/README.md` file to discover where the change or newly established knowledge should be documented. If the changes you make are in conflict with previously documented behavior, update the document.

## Feed back what was missing

When you query the documentation for knowledge about physics, generator behaviors or framework behaviors, and the documentation was not able to answer the question, you have discovered a *knowledge gap*. If you are able to answer the question by doing exploration outside of the documentation, you have established *new knowledge*.
Feed the new knowledge into the **most appropriate existing file** before the session ends.

- This is a **merge, not an append**. Put the fact where the topic already lives
  and sharpen the surrounding sentences rather than adding a second statement
  beside them, so the section still reads as one explanation.
- Create a new file only when no existing one fits. A new file means a new row in
  `docs/README.md` in the same edit.

## Keep docs in sync with code

Changing behaviour that a `docs/` file describes means updating that file **in
the same session**. Two cases route to more than one document, and are easy to
half-do:

| Change | Also update |
|---|---|
| Normalization, `xsec_weight`, kinematics, merging | `docs/physics.md` **and** the affected `docs/generators/<gen>.md` |
| Container runtimes, image naming, composition | `docs/containers.md` **and** `docs/apptainer_image.md` |

Architectural rationale — why an approach was chosen and what was ruled out —
goes in `docs/design_decisions.md` as a `## <subject> (YYYY-MM)` section
appended at the end, or in the topic document with a dated pointer left in the
log.

## The evidence bar for domain facts

**Only record what direct evidence substantiates**, and say what the evidence
was:

- generator source code, a `.def` or Dockerfile, a shipped card or config file;
- actual generator output, inspected;
- a measured number from a run, quoted with the conditions that produced it
  (generator version, target, flux, event count);
- a published paper in the local literature database, `literature/`, cited —
  see below for what it can and cannot substantiate.

Speculation and recollection do not go in, and upstream documentation that has
not been checked against this codebase counts as speculation. A fact that
matters but is not yet substantiated belongs in `.opencode/TODOS.md` as something
to verify.

### Citing the literature

A paper in `literature/` substantiates **physics** — a definition, a formalism,
a measured quantity, why a model behaves as it does. It does not substantiate a
claim about this codebase or about our build of a generator. What a generator's
own paper says the generator does stays speculation until the source, the
shipped card or the inspected output confirms it here; the paper explains the
physics behind what those show.

Every claim taken from the literature carries its source in the sentence or as a
trailing parenthetical: `(Pandey 2025, arXiv:2511.05413, §4.2)`. The arXiv id is
not optional. `literature/` is gitignored, so a reader of `docs/` may not have
the paper — the id is what keeps the claim checkable.

## No agent instructions in `docs/`

`docs/` addresses humans and agents as *readers*. Directives to agents belong in
`AGENTS.md`, in `.opencode/`, or in this skill. Phrase invariants as facts about
the code — "the branch order is load-bearing because Apptainer cannot nest", not
"do not reorder these branches".
