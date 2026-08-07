"""Unified-diff parsing helpers.

GitHub only accepts inline review comments on lines that appear in the PR
diff, so we parse the diff once and validate the model's proposed comment
locations against it before posting.
"""

from __future__ import annotations

import re

HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def commentable_lines(diff: str) -> dict[str, set[int]]:
    """Map each file path to the set of new-side (RIGHT) line numbers that can
    carry an inline review comment: added and context lines within hunks."""
    result: dict[str, set[int]] = {}
    current: str | None = None
    new_line = 0
    in_hunk = False
    for raw in diff.splitlines():
        if raw.startswith("+++ "):
            path = raw[4:].strip()
            current = None if path == "/dev/null" else path.removeprefix("b/")
            in_hunk = False
            continue
        m = HUNK_RE.match(raw)
        if m:
            new_line = int(m.group(1))
            in_hunk = True
            continue
        if not in_hunk or current is None:
            continue
        if raw.startswith("+") or raw.startswith(" ") or raw == "":
            result.setdefault(current, set()).add(new_line)
            new_line += 1
        elif raw.startswith("\\"):  # "\ No newline at end of file"
            continue
        elif raw.startswith("-"):
            continue
        else:  # left the hunk (e.g. "diff --git" of the next file)
            in_hunk = False
    return result


def per_file_diffs(diff: str) -> dict[str, str]:
    """Split a unified diff into {path: file_diff} chunks."""
    chunks: dict[str, str] = {}
    current_path: str | None = None
    current_lines: list[str] = []
    for raw in diff.splitlines():
        if raw.startswith("diff --git "):
            if current_path is not None:
                chunks[current_path] = "\n".join(current_lines)
            current_lines = [raw]
            # "diff --git a/x b/y" — take the b/ path (post-change name)
            parts = raw.split(" b/", 1)
            current_path = parts[1] if len(parts) == 2 else raw
        else:
            current_lines.append(raw)
    if current_path is not None:
        chunks[current_path] = "\n".join(current_lines)
    return chunks
