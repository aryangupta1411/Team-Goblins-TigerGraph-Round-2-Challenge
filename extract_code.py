r"""Materialise the repository tree from IMPLEMENTATION.md.

The specification embeds every source file verbatim: a heading of the form

    ### `etl/features.py`

is followed (within five lines) by a fenced code block holding that file's exact contents.
This script walks the document and writes each block to its path.

    python extract_code.py                  # writes ./ (dry run first with --dry-run)
    python extract_code.py --out build/     # writes into build/
    python extract_code.py --dry-run        # list what would be written

Files already on disk are only overwritten with --force, so re-running after local edits is safe.
"""
from __future__ import annotations

import argparse
import pathlib
import re
import sys

HEADING = re.compile(r"^#{3,4}\s+`([^`]+)`\s*$")  # ### in a module spec, #### in the assembled document
FENCE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")
# A heading is a file only if it looks like a path with a known source extension.
SUFFIXES = {
    ".py", ".gsql", ".yaml", ".yml", ".sql", ".json", ".toml", ".md", ".txt",
    ".cfg", ".ini", ".sh", ".env", ".example", ".gitignore", ".jsonl", ".csv",
}


def looks_like_file(rel: str) -> bool:
    if rel.startswith(("http://", "https://")) or " " in rel.strip():
        return False
    name = rel.rsplit("/", 1)[-1]
    if name.startswith(".") and "." not in name[1:]:
        return True  # .gitignore, .env
    return pathlib.PurePosixPath(rel).suffix in SUFFIXES or name in {"Makefile", "Dockerfile"}


def blocks(text: str):
    """Yield (path, body) for every '### `path`' heading followed by a fenced block."""
    lines = text.split("\n")
    i = 0
    while i < len(lines):
        m = HEADING.match(lines[i])
        if not m:
            i += 1
            continue
        rel = m.group(1).strip()
        j = i + 1
        while j < len(lines) and j < i + 6 and not FENCE.match(lines[j]):
            j += 1
        if j >= len(lines) or not FENCE.match(lines[j]):
            i += 1
            continue
        tok = FENCE.match(lines[j]).group(1)
        # Scan for the closing fence, but never past the next file heading: a block cannot span one.
        # If one arrives first, the block was opened with a longer fence than it was closed with, so
        # fall back to the last fence-looking line before that heading.
        k, last_fence = j + 1, None
        while k < len(lines):
            m2 = FENCE.match(lines[k])
            if m2 and m2.group(1)[0] == tok[0] and lines[k].strip() == m2.group(1):
                if len(m2.group(1)) >= len(tok):
                    break
                last_fence = k
            elif HEADING.match(lines[k]):
                k = last_fence if last_fence is not None else k
                break
            k += 1
        if k < len(lines) and looks_like_file(rel):
            yield rel, "\n".join(lines[j + 1:k])
        i = k + 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--spec", default=str(pathlib.Path(__file__).with_name("IMPLEMENTATION.md")))
    ap.add_argument("--out", default=".")
    ap.add_argument("--force", action="store_true", help="overwrite files that already exist")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    spec = pathlib.Path(args.spec)
    if not spec.exists():
        print(f"spec not found: {spec}", file=sys.stderr)
        return 2
    out_root = pathlib.Path(args.out)

    written, skipped, dupes = 0, 0, {}
    for rel, body in blocks(spec.read_text()):
        dupes[rel] = dupes.get(rel, 0) + 1
        dest = out_root / rel
        if args.dry_run:
            print(f"{'dup ' if dupes[rel] > 1 else '    '}{rel}  ({len(body.splitlines())} lines)")
            continue
        if dest.exists() and not args.force:
            skipped += 1
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(body.rstrip("\n") + "\n")
        written += 1
    repeats = {k: v for k, v in dupes.items() if v > 1}
    print(f"{len(dupes)} file blocks; written {written}, skipped (exists) {skipped}"
          + (f"; {len(repeats)} paths appear more than once (last wins)" if repeats else ""))
    for k in list(repeats)[:10]:
        print("   repeated:", k)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
