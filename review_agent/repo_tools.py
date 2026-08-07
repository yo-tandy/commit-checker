"""Local-repository tools the review agent can call while reviewing.

The action runs inside the consuming repo's workflow after actions/checkout,
so the working tree is the PR head. All paths are confined to the repo root.
"""

from __future__ import annotations

import fnmatch
import subprocess
from pathlib import Path

MAX_RESULT_CHARS = 20_000


def _clip(text: str) -> str:
    if len(text) <= MAX_RESULT_CHARS:
        return text
    return (
        text[:MAX_RESULT_CHARS]
        + f"\n... [truncated at {MAX_RESULT_CHARS} characters — narrow the request to see more]"
    )


class RepoTools:
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()

    def _resolve(self, path: str) -> Path:
        resolved = (self.root / path).resolve()
        if resolved != self.root and self.root not in resolved.parents:
            raise ValueError(f"path escapes the repository root: {path}")
        return resolved

    def list_files(self, glob: str = "") -> str:
        proc = subprocess.run(
            ["git", "ls-files"], cwd=self.root, capture_output=True, text=True
        )
        if proc.returncode != 0:
            files = sorted(
                str(p.relative_to(self.root))
                for p in self.root.rglob("*")
                if p.is_file() and ".git" not in p.parts
            )
        else:
            files = proc.stdout.splitlines()
        if glob:
            files = [f for f in files if fnmatch.fnmatch(f, glob)]
        return _clip("\n".join(files) or "(no files matched)")

    def read_file(self, path: str, start_line: int = 1, end_line: int = 0) -> str:
        target = self._resolve(path)
        if not target.is_file():
            return f"error: {path} is not a file"
        lines = target.read_text(errors="replace").splitlines()
        end = end_line if end_line > 0 else len(lines)
        selected = lines[max(start_line - 1, 0) : end]
        numbered = "\n".join(
            f"{i}\t{line}" for i, line in enumerate(selected, start=max(start_line, 1))
        )
        return _clip(numbered or "(empty range)")

    def search(self, pattern: str, glob: str = "") -> str:
        cmd = ["git", "grep", "-n", "-I", "--untracked", "-e", pattern]
        if glob:
            cmd += ["--", glob]
        proc = subprocess.run(cmd, cwd=self.root, capture_output=True, text=True)
        if proc.returncode == 1:
            return "(no matches)"
        if proc.returncode != 0:
            # Not a git checkout (e.g. local run) — fall back to plain grep.
            cmd = ["grep", "-rnI", "--exclude-dir=.git", "-e", pattern]
            if glob:
                cmd.append(f"--include={glob.rsplit('/', 1)[-1]}")
            cmd.append(".")
            proc = subprocess.run(cmd, cwd=self.root, capture_output=True, text=True)
            if proc.returncode == 1:
                return "(no matches)"
            if proc.returncode != 0:
                return f"error: {proc.stderr.strip()}"
        return _clip(proc.stdout)
