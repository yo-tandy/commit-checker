"""The Claude review loop: explore the repo with tools, then submit a verdict."""

from __future__ import annotations

import json
import logging

import anthropic

from .diff_utils import per_file_diffs
from .repo_tools import RepoTools

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-opus-5"
MAX_TURNS = 40
INLINE_DIFF_LIMIT = 60_000  # above this, the model pulls per-file diffs on demand

SYSTEM_PROMPT = """\
You are an automated senior code reviewer for GitHub pull requests. You review \
the full change in context: you can list, read, and search any file in the \
repository (checked out at the PR head), not just the diff.

Review the pull request along four dimensions:

1. Commit quality — are the commits coherent and well-scoped, with messages \
that accurately describe the change? Flag stray artifacts (debug prints, \
commented-out code, generated files, committed secrets or credentials).
2. Documentation — when behavior, configuration, or public interfaces change, \
were the relevant docs updated (README, docstrings, comments, changelogs, API \
docs)? Missing doc updates for user-visible changes are a real finding.
3. Testing — is new or changed behavior covered by tests? Are the tests \
meaningful (asserting behavior, not just executing code)? Look at the existing \
test conventions in the repo before judging.
4. Security — injection risks, missing input validation at trust boundaries, \
authn/authz gaps, secrets in code or config, unsafe deserialization, path \
traversal, risky dependency or workflow changes.

Process:
- Start from the diff and commit list, then read the surrounding code you need \
to judge correctness and context. Check whether tests and docs elsewhere in \
the repo already cover the change before flagging their absence.
- Report every issue you find, including ones you are uncertain about or \
consider low-severity, and state your confidence and severity for each. Do not \
pad with style nits that don't affect correctness, security, or maintainability.
- When you are done, call submit_review exactly once. Do not end your turn \
without calling it.

Verdict criteria:
- "request_changes": a probable bug, a security risk, a committed secret, or \
substantive changed behavior with no test coverage at all.
- "approve": the change is correct and reasonably tested/documented; only \
minor or optional suggestions remain.
- "comment": you have observations worth sharing but nothing that clearly \
blocks, or you could not verify enough to judge confidently.

Inline comments must point at a file path and new-side line number that appears \
in the PR diff; anything about the change as a whole belongs in the summary. \
Write the summary for the PR author: lead with the verdict rationale, then a \
short assessment per dimension.\
"""

TOOLS = [
    {
        "name": "list_files",
        "description": "List files tracked in the repository. Optionally filter with a glob pattern such as '*.py' or 'src/*'.",
        "input_schema": {
            "type": "object",
            "properties": {
                "glob": {"type": "string", "description": "Optional fnmatch-style filter."}
            },
        },
    },
    {
        "name": "read_file",
        "description": "Read a file from the repository at the PR head, with line numbers. Use start_line/end_line to read a slice of a large file.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "start_line": {"type": "integer", "description": "1-based, default 1."},
                "end_line": {"type": "integer", "description": "Inclusive; 0 means end of file."},
            },
            "required": ["path"],
        },
    },
    {
        "name": "search",
        "description": "Search file contents with a regex (git grep -n). Returns matching lines with file and line number. Optionally restrict to a path glob.",
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string"},
                "glob": {"type": "string", "description": "Optional path filter, e.g. 'tests/*'."},
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "get_file_diff",
        "description": "Return the PR diff for a single file. Use this when the full diff was too large to include up front.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
    {
        "name": "submit_review",
        "description": "Submit the final review. Call exactly once, after your investigation is complete.",
        "input_schema": {
            "type": "object",
            "properties": {
                "verdict": {
                    "type": "string",
                    "enum": ["approve", "request_changes", "comment"],
                },
                "summary": {
                    "type": "string",
                    "description": "Markdown review body: verdict rationale, then commit quality / documentation / testing / security assessments.",
                },
                "comments": {
                    "type": "array",
                    "description": "Inline comments anchored to the diff.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string", "description": "Repo-relative file path."},
                            "line": {"type": "integer", "description": "New-side line number appearing in the diff."},
                            "body": {"type": "string", "description": "The comment, including severity and confidence."},
                        },
                        "required": ["path", "line", "body"],
                    },
                },
            },
            "required": ["verdict", "summary"],
        },
    },
]


def build_initial_prompt(pr: dict, commits: list[dict], diff: str) -> str:
    commit_lines = []
    for c in commits:
        sha = c["sha"][:10]
        message = c["commit"]["message"]
        commit_lines.append(f"- {sha} {message}")
    parts = [
        f"Review pull request #{pr['number']}: {pr['title']}",
        f"Base: {pr['base']['ref']}  Head: {pr['head']['ref']}",
        f"PR description:\n{pr.get('body') or '(none)'}",
        f"Commits ({len(commits)}):\n" + "\n".join(commit_lines),
    ]
    if len(diff) <= INLINE_DIFF_LIMIT:
        parts.append(f"Full diff:\n```diff\n{diff}\n```")
    else:
        files = sorted(per_file_diffs(diff))
        parts.append(
            "The diff is too large to include inline. Changed files are listed "
            "below; fetch each one you review with get_file_diff:\n"
            + "\n".join(f"- {f}" for f in files)
        )
    return "\n\n".join(parts)


class ReviewAgent:
    def __init__(self, repo_root: str, diff: str, model: str = DEFAULT_MODEL):
        self.client = anthropic.Anthropic()
        self.model = model
        self.tools = RepoTools(repo_root)
        self.file_diffs = per_file_diffs(diff)

    def _dispatch(self, name: str, tool_input: dict) -> str:
        try:
            if name == "list_files":
                return self.tools.list_files(tool_input.get("glob", ""))
            if name == "read_file":
                return self.tools.read_file(
                    tool_input["path"],
                    tool_input.get("start_line", 1),
                    tool_input.get("end_line", 0),
                )
            if name == "search":
                return self.tools.search(
                    tool_input["pattern"], tool_input.get("glob", "")
                )
            if name == "get_file_diff":
                path = tool_input["path"]
                return self.file_diffs.get(path, f"error: no diff for {path}")
            return f"error: unknown tool {name}"
        except Exception as exc:  # tool errors go back to the model, not up the stack
            return f"error: {exc}"

    def run(self, initial_prompt: str) -> dict:
        """Drive the agentic loop until submit_review is called. Returns the
        review dict: {verdict, summary, comments}."""
        messages: list[dict] = [{"role": "user", "content": initial_prompt}]
        for turn in range(MAX_TURNS):
            response = self.client.beta.messages.create(
                model=self.model,
                max_tokens=16000,
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
                output_config={"effort": "high"},
                cache_control={"type": "ephemeral"},
                system=SYSTEM_PROMPT,
                tools=TOOLS,
                messages=messages,
            )

            if response.stop_reason == "refusal":
                raise RuntimeError(
                    "The model (and its fallback chain) declined to review this PR: "
                    f"{getattr(response.stop_details, 'explanation', None) or 'no explanation provided'}"
                )

            messages.append({"role": "assistant", "content": response.content})
            tool_uses = [b for b in response.content if b.type == "tool_use"]

            if not tool_uses:
                # Ended without submitting — nudge once per occurrence.
                messages.append(
                    {
                        "role": "user",
                        "content": "You have not called submit_review yet. "
                        "Finish your investigation and call submit_review with your verdict.",
                    }
                )
                continue

            results = []
            review: dict | None = None
            for tu in tool_uses:
                if tu.name == "submit_review":
                    review = dict(tu.input)
                    result = "Review recorded."
                else:
                    logger.info("tool call %s(%s)", tu.name, json.dumps(tu.input)[:200])
                    result = self._dispatch(tu.name, dict(tu.input))
                results.append(
                    {"type": "tool_result", "tool_use_id": tu.id, "content": result}
                )
            messages.append({"role": "user", "content": results})
            if review is not None:
                return review

        raise RuntimeError(f"review did not complete within {MAX_TURNS} turns")
