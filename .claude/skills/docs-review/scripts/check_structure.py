#!/usr/bin/env python3
"""Mechanical structure checks for docs/*.md. Read-only; prints findings.

Usage: check_structure.py [FILE.md ...]   (default: every .md under docs/)

Reports, per file:
  - line count and whether a TOC (a list of links to own anchors before the
    first H2) is present;
  - TOC entries whose anchor matches no heading, and H2 headings missing from
    the TOC;
  - relative links to files that do not exist, or to anchors those files lack;
  - for docs/generators/*.md, the H2 sequence next to each other, so ordering
    drift against the template in docs/adding_generators.md is visible.
"""
import os
import re
import sys
from pathlib import Path

LONG = 200  # lines; above this a document should open with a TOC
LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")
HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")


def slug(text):
    """GitHub's anchor for a heading."""
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)  # keep link text
    text = text.replace("`", "").strip().lower()
    text = re.sub(r"[^\w\- ]", "", text)
    return text.replace(" ", "-")


def parse(path):
    """Return (lines, headings[(level, text, line_no)], anchors set)."""
    lines = path.read_text().splitlines()
    headings, fenced = [], False
    for i, line in enumerate(lines, 1):
        if line.lstrip().startswith("```"):
            fenced = not fenced
            continue
        m = None if fenced else HEADING.match(line)
        if m:
            headings.append((len(m.group(1)), m.group(2), i))
    anchors, seen = set(), {}
    for _, text, _ in headings:
        s = slug(text)
        n = seen.get(s, 0)
        anchors.add(s if n == 0 else f"{s}-{n}")
        seen[s] = n + 1
    return lines, headings, anchors


def check(path, cache):
    lines, headings, anchors = cache[path]
    out = []
    h2 = [(t, n) for lvl, t, n in headings if lvl == 2]
    first_h2 = h2[0][1] if h2 else len(lines) + 1
    # TOC = list items linking to own anchors; inline links in prose don't count
    toc = [m.group(1)[1:] for line in lines[: first_h2 - 1]
           if re.match(r"\s*([-*+]|\d+\.)\s", line)
           for m in LINK.finditer(line) if m.group(1).startswith("#")]
    if len(lines) > LONG and not toc:
        out.append(f"{len(lines)} lines, no TOC before the first H2 (line {first_h2})")
    if toc:
        for a in toc:
            if a not in anchors:
                out.append(f"TOC entry #{a} matches no heading")
        for t, n in h2:
            if slug(t) not in toc:
                out.append(f"line {n}: H2 '{t}' missing from TOC")
    fenced = False
    for i, line in enumerate(lines, 1):
        if line.lstrip().startswith("```"):
            fenced = not fenced
        if fenced:
            continue
        for m in LINK.finditer(line):
            target = m.group(1)
            if re.match(r"[a-z]+:", target):
                continue  # external URL / mailto
            file_part, _, anchor = target.partition("#")
            dest = (path.parent / file_part).resolve() if file_part else path
            if file_part and not dest.exists():
                out.append(f"line {i}: link target missing: {target}")
                continue
            if anchor and dest.suffix == ".md":
                if dest not in cache:
                    cache[dest] = parse(dest)
                if anchor not in cache[dest][2]:
                    out.append(f"line {i}: anchor not found: {target}")
    return out


def main():
    root = Path(__file__).resolve().parents[4]
    files = [Path(a).resolve() for a in sys.argv[1:]] or sorted((root / "docs").rglob("*.md"))
    cache = {f: parse(f) for f in files}
    for f in files:
        findings = check(f, cache)
        print(f"== {os.path.relpath(f, root)} ({len(cache[f][0])} lines)")
        for x in findings:
            print(f"  - {x}")
    gens = [f for f in files if f.parent.name == "generators"]
    if gens:
        print("\n== generator-doc H2 sequences (compare with docs/adding_generators.md template)")
        for f in gens:
            seq = [t for lvl, t, _ in cache[f][1] if lvl == 2]
            print(f"  {f.stem}: " + " | ".join(seq))


if __name__ == "__main__":
    main()
