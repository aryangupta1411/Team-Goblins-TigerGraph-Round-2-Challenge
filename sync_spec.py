r"""Write edited source files back into the specification.

`IMPLEMENTATION.md` (and each module spec it is assembled from) carries every source file verbatim
under a heading of the form ``### `repo/path` `` followed by a fenced block. This script refreshes
those blocks from the files on disk, so the document never drifts from the code.

    python sync_spec.py                 # refresh every module spec next to this file
    python sync_spec.py 40-engine.md    # or just one

`extract_code.py` is the inverse: it writes the tree out of the document.
"""
from __future__ import annotations

import pathlib
import re
import sys

HERE = pathlib.Path(__file__).parent
# In the spec workspace the sources live under impl/code/; in the repository they sit at the root.
CODE = HERE / "code" if (HERE / "code").is_dir() else HERE
HEADING = re.compile(r"^#{3,4}\s+`([^`]+)`\s*$")
FENCE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")
ALT_PREFIXES = ("root", "docs", "root/docs")


def resolve(rel: str) -> pathlib.Path | None:
    """Find the source file for a heading path, allowing the spec's root/ and docs/ staging dirs."""
    direct = CODE / rel
    if direct.is_file():
        return direct
    for prefix in ALT_PREFIXES:
        candidate = CODE / prefix / rel
        if candidate.is_file():
            return candidate
    return None


def sync(spec_path: pathlib.Path) -> tuple[int, list[str]]:
    """Replace each file block in one spec with the current file contents."""
    lines = spec_path.read_text().split("\n")
    out: list[str] = []
    missing: list[str] = []
    replaced = 0
    i = 0
    while i < len(lines):
        heading = HEADING.match(lines[i])
        out.append(lines[i])
        i += 1
        if not heading:
            continue
        rel = heading.group(1).strip()

        # The fence opens within a few lines of the heading; anything before it is prose.
        j = i
        while j < len(lines) and j < i + 5 and not FENCE.match(lines[j]):
            out.append(lines[j])
            j += 1
        if j >= len(lines) or not FENCE.match(lines[j]):
            continue

        token = FENCE.match(lines[j]).group(1)
        k = j + 1
        while k < len(lines):
            close = FENCE.match(lines[k])
            if close and close.group(1)[0] == token[0] and len(close.group(1)) >= len(token) \
                    and lines[k].strip() == close.group(1):
                break
            k += 1
        if k >= len(lines):
            out.extend(lines[j:])
            i = len(lines)
            continue

        src = resolve(rel)
        if src is None:
            out.extend(lines[j:k + 1])
            missing.append(rel)
        else:
            out.append(lines[j])
            out.extend(src.read_text().rstrip("\n").split("\n"))
            out.append(lines[k])
            replaced += 1
        i = k + 1

    spec_path.write_text("\n".join(out))
    return replaced, missing


def main() -> int:
    targets = sys.argv[1:] or sorted(p.name for p in HERE.glob("[0-7]0-*.md"))
    if not targets:
        print("no spec files found next to sync_spec.py", file=sys.stderr)
        return 2
    for name in targets:
        path = HERE / name
        if not path.exists():
            print(f"{name}: not found", file=sys.stderr)
            continue
        replaced, missing = sync(path)
        print(f"{name}: {replaced} blocks refreshed, {len(missing)} headings without a source file")
        for rel in missing[:10]:
            print("   missing:", rel)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
