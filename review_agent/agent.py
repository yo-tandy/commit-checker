"""Claude agent loops: the PR reviewer and the comment-thread responder."""

from __future__ import annotations

import json
import logging

import anthropic

from .diff_utils import per_file_diffs
from .repo_tools import RepoTools

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-opus-5"
INLINE_DIFF_LIMIT = 60_000  # above this, the model pulls per-file diffs on demand

REVIEW_SYSTEM_PROMPT = """\
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
- Batch independent tool calls in a single turn — fetch several file diffs or \
read several files at once. Your budget is a limited number of turns, not a \
limited number of tool calls, so serial one-call turns waste it.
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

RESPONDER_SYSTEM_PROMPT = """\
You are the automated code reviewer that previously reviewed this pull \
request. A human has replied in a thread on one of your inline review \
comments. Decide whether a response is needed, and respond only when it adds \
value — silence is the right answer for pure acknowledgments.

You can list, read, and search the repository at the PR's current head, and \
fetch the current PR diff per file. Verify claims against the code before \
responding.

Guidelines:
- Reply claims the issue is fixed: check the current code. If it is fixed, \
post a one-line confirmation. If it is partially fixed or not fixed, say \
precisely what is still missing, citing the file and line. If you cannot find \
the fix at all, say so and note the commit may not be pushed yet.
- Reply asks a question: answer it concretely, grounded in the actual code.
- Reply disagrees with your finding: re-examine with fresh eyes. If they are \
right, concede plainly. If the finding stands, explain why once, with \
evidence — do not restate your original comment. At most one round of \
pushback; if the author has clearly made their decision, accept it.
- Pure acknowledgment with nothing to verify or answer ("ack", "will do", \
an emoji): call no_response.

Keep replies to a few sentences, specific and collegial — you are talking to \
the PR author in a public thread. Finish by calling exactly one of post_reply \
or no_response.\
"""

COMMON_TOOLS = [
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
        "description": "Return the PR diff for a single file.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
]

SUBMIT_REVIEW_TOOL = {
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
}

POST_REPLY_TOOL = {
    "name": "post_reply",
    "description": "Post a reply in the review comment thread. Call at most once.",
    "input_schema": {
        "type": "object",
        "properties": {
            "body": {"type": "string", "description": "Markdown reply, a few sentences."}
        },
        "required": ["body"],
    },
}

NO_RESPONSE_TOOL = {
    "name": "no_response",
    "description": "Decide that no reply is needed for this thread.",
    "input_schema": {
        "type": "object",
        "properties": {
            "reason": {"type": "string", "description": "One line explaining why (for the workflow log only)."}
        },
        "required": ["reason"],
    },
}


class ToolLoopAgent:
    """Generic agentic loop over the repo tools plus a set of terminal tools.

    run() drives the conversation until the model calls a terminal tool, then
    returns (terminal_tool_name, tool_input).
    """

    def __init__(
        self,
        repo_root: str,
        diff: str,
        system_prompt: str,
        terminal_tools: list[dict],
        model: str = DEFAULT_MODEL,
        max_turns: int = 40,
    ):
        self.client = anthropic.Anthropic()
        self.model = model
        self.max_turns = max_turns
        self.system_prompt = system_prompt
        self.tool_defs = COMMON_TOOLS + terminal_tools
        self.terminal_names = {t["name"] for t in terminal_tools}
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

    def run(self, initial_prompt: str) -> tuple[str, dict]:
        messages: list[dict] = [{"role": "user", "content": initial_prompt}]
        terminal_list = " or ".join(sorted(self.terminal_names))
        for _ in range(self.max_turns):
            response = self.client.beta.messages.create(
                model=self.model,
                max_tokens=16000,
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
                output_config={"effort": "high"},
                cache_control={"type": "ephemeral"},
                system=self.system_prompt,
                tools=self.tool_defs,
                messages=messages,
            )

            if response.stop_reason == "refusal":
                raise RuntimeError(
                    "The model (and its fallback chain) declined this task: "
                    f"{getattr(response.stop_details, 'explanation', None) or 'no explanation provided'}"
                )

            messages.append({"role": "assistant", "content": response.content})
            tool_uses = [b for b in response.content if b.type == "tool_use"]

            if not tool_uses:
                messages.append(
                    {
                        "role": "user",
                        "content": f"You have not finished: call {terminal_list} to complete the task.",
                    }
                )
                continue

            results = []
            terminal: tuple[str, dict] | None = None
            for tu in tool_uses:
                if tu.name in self.terminal_names:
                    terminal = (tu.name, dict(tu.input))
                    result = "Recorded."
                else:
                    logger.info("tool call %s(%s)", tu.name, json.dumps(tu.input)[:200])
                    result = self._dispatch(tu.name, dict(tu.input))
                results.append(
                    {"type": "tool_result", "tool_use_id": tu.id, "content": result}
                )
            if terminal is not None:
                messages.append({"role": "user", "content": results})
                return terminal
            remaining = self.max_turns - turn - 1
            if remaining <= 3:
                results.append(
                    {
                        "type": "text",
                        "text": f"[turn budget] Only {remaining} turn(s) remain. "
                        f"Stop investigating and call {terminal_list} now, based on "
                        "what you have already learned — an incomplete-but-submitted "
                        "assessment is required; note any areas you could not verify.",
                    }
                )
            messages.append({"role": "user", "content": results})

        raise RuntimeError(f"agent did not complete within {self.max_turns} turns")


def build_review_prompt(pr: dict, commits: list[dict], diff: str) -> str:
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


def build_responder_prompt(pr: dict, thread: list[dict], trigger: dict) -> str:
    thread_lines = []
    for c in thread:
        author = c["user"]["login"]
        marker = " (this is you, the reviewer)" if author.endswith("[bot]") else ""
        thread_lines.append(f"--- {author}{marker} at {c['created_at']}:\n{c['body']}")
    root = thread[0]
    return "\n\n".join(
        [
            f"Pull request #{pr['number']}: {pr['title']} "
            f"(base {pr['base']['ref']}, head {pr['head']['ref']})",
            f"The thread is anchored at `{root['path']}` line {root.get('line') or root.get('original_line')}."
            f"\nDiff hunk at the time of the original comment:\n```diff\n{root.get('diff_hunk', '')}\n```",
            "Full thread, oldest first:\n" + "\n\n".join(thread_lines),
            f"The reply you are reacting to is the one from {trigger['user']['login']} "
            f"at {trigger['created_at']}. Investigate as needed, then call "
            "post_reply or no_response.",
        ]
    )


def review_turn_budget(diff: str) -> int:
    """Scale the turn budget with PR size so large diffs get room to breathe."""
    n_files = max(1, len(per_file_diffs(diff)))
    return min(120, 30 + 3 * n_files)


def run_review(
    repo_root: str, diff: str, pr: dict, commits: list[dict], model: str = DEFAULT_MODEL
) -> dict:
    agent = ToolLoopAgent(
        repo_root,
        diff,
        REVIEW_SYSTEM_PROMPT,
        [SUBMIT_REVIEW_TOOL],
        model=model,
        max_turns=review_turn_budget(diff),
    )
    _, review = agent.run(build_review_prompt(pr, commits, diff))
    return review


def run_responder(
    repo_root: str,
    diff: str,
    pr: dict,
    thread: list[dict],
    trigger: dict,
    model: str = DEFAULT_MODEL,
) -> tuple[str, dict]:
    agent = ToolLoopAgent(
        repo_root,
        diff,
        RESPONDER_SYSTEM_PROMPT,
        [POST_REPLY_TOOL, NO_RESPONSE_TOOL],
        model=model,
        max_turns=20,
    )
    return agent.run(build_responder_prompt(pr, thread, trigger))
