"""Review and responder runs, executed by Claude Code (CLI) in headless mode.

The wrapper prepares context files (PR metadata, diff, thread), invokes
`claude -p` with a structured-output schema and read-only tools, and parses
the JSON result. Claude Code supplies the agent loop and its own
Read/Grep/Glob/Bash tools over the checked-out repo.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
from pathlib import Path

from .diff_utils import per_file_diffs

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-opus-5"
CLI_TIMEOUT_SECONDS = 3600

# Read-only exploration: Claude Code's file tools plus scoped git commands.
# No Edit/Write, and --permission-mode dontAsk denies anything not listed.
ALLOWED_TOOLS = (
    "Read,Grep,Glob,"
    "Bash(git diff *),Bash(git log *),Bash(git show *),Bash(git ls-files *),Bash(ls *)"
)

REVIEW_SYSTEM_PROMPT = """\
You are an automated senior code reviewer for GitHub pull requests. You review \
the full change in context: the repository is checked out at the PR head, and \
you may read and search any of it — not just the diff.

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

Report every issue you find, including ones you are uncertain about or \
consider low-severity, and state your confidence and severity for each. Do not \
pad with style nits that don't affect correctness, security, or \
maintainability. Check whether tests and docs elsewhere in the repo already \
cover the change before flagging their absence.

Verdict criteria:
- "request_changes": a probable bug, a security risk, a committed secret, or \
substantive changed behavior with no test coverage at all.
- "approve": the change is correct and reasonably tested/documented; only \
minor or optional suggestions remain.
- "comment": you have observations worth sharing but nothing that clearly \
blocks, or you could not verify enough to judge confidently.

Inline comments must point at a file path and new-side line number that \
appears in the PR diff; anything about the change as a whole belongs in the \
summary. Write the summary for the PR author: lead with the verdict \
rationale, then a short assessment per dimension.\
"""

RESPONDER_SYSTEM_PROMPT = """\
You are the automated code reviewer that previously reviewed this pull \
request. A human has replied in a thread on one of your inline review \
comments. Decide whether a response is needed, and respond only when it adds \
value — silence is the right answer for pure acknowledgments.

The repository is checked out at the PR's current head. Verify claims against \
the code before responding.

Guidelines:
- Reply claims the issue is fixed: check the current code. If it is fixed, \
respond with a one-line confirmation. If it is partially fixed or not fixed, \
say precisely what is still missing, citing the file and line. If you cannot \
find the fix at all, say so and note the commit may not be pushed yet.
- Reply asks a question: answer it concretely, grounded in the actual code.
- Reply disagrees with your finding: re-examine with fresh eyes. If they are \
right, concede plainly. If the finding stands, explain why once, with \
evidence — do not restate your original comment. At most one round of \
pushback; if the author has clearly made their decision, accept it.
- Pure acknowledgment with nothing to verify or answer ("ack", "will do", \
an emoji): choose no_response.

Keep replies to a few sentences, specific and collegial — you are talking to \
the PR author in a public thread.\
"""

REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["approve", "request_changes", "comment"]},
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
                "additionalProperties": False,
            },
        },
    },
    "required": ["verdict", "summary"],
    "additionalProperties": False,
}

RESPONDER_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["post_reply", "no_response"]},
        "body": {
            "type": "string",
            "description": "Markdown reply to post (required when action is post_reply).",
        },
        "reason": {
            "type": "string",
            "description": "One line explaining a no_response decision (workflow log only).",
        },
    },
    "required": ["action"],
    "additionalProperties": False,
}


def review_turn_budget(diff: str) -> int:
    """Scale the turn budget with PR size so large diffs get room to breathe."""
    n_files = max(1, len(per_file_diffs(diff)))
    return min(120, 30 + 3 * n_files)


def _extract_structured(stdout: str) -> dict:
    """Pull the structured result out of `claude --output-format json` output."""
    data = json.loads(stdout)
    if data.get("is_error"):
        raise RuntimeError(f"claude reported an error: {data.get('result', '')[:500]}")
    structured = data.get("structured_output")
    if isinstance(structured, dict):
        return structured
    # Fallback: the result text itself should be JSON (possibly fenced).
    text = (data.get("result") or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text.split("\n", 1)[1] if "\n" in text else text
    return json.loads(text)


def run_claude(
    prompt: str,
    system_prompt: str,
    schema: dict,
    repo_root: str,
    context_dir: str,
    model: str,
    max_turns: int,
) -> dict:
    claude_bin = os.environ.get("CLAUDE_BIN", "claude")
    with tempfile.NamedTemporaryFile(
        "w", suffix=".txt", delete=False, prefix="commit-checker-system-"
    ) as f:
        f.write(system_prompt)
        system_file = f.name
    cmd = [
        claude_bin,
        # Load only user-level settings (absent on a fresh CI runner) so the
        # reviewed repo's hooks/settings can't influence its own reviewer.
        # Deliberately NOT --bare: bare mode skips CLAUDE_CODE_OAUTH_TOKEN
        # resolution, which breaks subscription auth in CI.
        "--setting-sources", "user",
        "-p", prompt,
        "--output-format", "json",
        "--json-schema", json.dumps(schema),
        "--permission-mode", "dontAsk",
        "--allowedTools", ALLOWED_TOOLS,
        "--max-turns", str(max_turns),
        "--model", model,
        "--append-system-prompt-file", system_file,
        "--add-dir", context_dir,
    ]
    budget = os.environ.get("MAX_BUDGET_USD", "").strip()
    if budget:
        cmd += ["--max-budget-usd", budget]

    logger.info("Invoking Claude Code (%s, max %d turns)", model, max_turns)
    try:
        proc = subprocess.run(
            cmd,
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=CLI_TIMEOUT_SECONDS,
        )
    finally:
        Path(system_file).unlink(missing_ok=True)

    if proc.returncode != 0:
        # The CLI emits its result JSON even on failure — surface the actual
        # error (e.g. "Credit balance is too low") instead of a JSON tail.
        detail = (proc.stderr or proc.stdout or "").strip()[-800:]
        try:
            data = json.loads(proc.stdout)
            reason = data.get("result") or data.get("terminal_reason") or ""
            if reason:
                detail = f"{reason} (api_error_status={data.get('api_error_status')})"
        except (json.JSONDecodeError, TypeError):
            pass
        raise RuntimeError(f"claude exited with {proc.returncode}: {detail}")
    result = _extract_structured(proc.stdout)
    try:
        cost = json.loads(proc.stdout).get("total_cost_usd")
        if cost is not None:
            logger.info("Claude Code run cost: $%.4f", cost)
    except (json.JSONDecodeError, TypeError):
        pass
    return result


def write_context_files(context_dir: str, files: dict[str, str]) -> dict[str, str]:
    paths = {}
    for name, content in files.items():
        path = Path(context_dir) / name
        path.write_text(content)
        paths[name] = str(path)
    return paths


def build_review_prompt(pr: dict, commits: list[dict], paths: dict[str, str]) -> str:
    return (
        f"Review pull request #{pr['number']}: {pr['title']}\n"
        f"Base: {pr['base']['ref']}  Head: {pr['head']['ref']}\n\n"
        "Start by reading these context files (outside the repo, already "
        "accessible to you):\n"
        f"- {paths['pr.md']} — PR description and the full commit list\n"
        f"- {paths['diff.patch']} — the complete PR diff (unified format)\n\n"
        "Then explore the repository as needed and produce your review as "
        "structured output matching the provided schema. Inline comment line "
        "numbers must be new-side lines present in the diff."
    )


def build_responder_prompt(pr: dict, trigger: dict, paths: dict[str, str]) -> str:
    return (
        f"A human replied to one of your review comments on pull request "
        f"#{pr['number']}: {pr['title']}.\n\n"
        "Read these context files first:\n"
        f"- {paths['thread.md']} — the full comment thread, oldest first\n"
        f"- {paths['diff.patch']} — the current PR diff\n\n"
        f"The reply to react to is the last one, from {trigger['user']['login']}. "
        "Investigate the repository as needed, then produce structured output: "
        "action=post_reply with a body, or action=no_response with a reason."
    )


def format_pr_context(pr: dict, commits: list[dict]) -> str:
    commit_lines = [f"- {c['sha'][:10]} {c['commit']['message']}" for c in commits]
    return (
        f"# PR #{pr['number']}: {pr['title']}\n\n"
        f"Base: {pr['base']['ref']}  Head: {pr['head']['ref']}\n\n"
        f"## Description\n\n{pr.get('body') or '(none)'}\n\n"
        f"## Commits ({len(commits)})\n\n" + "\n".join(commit_lines) + "\n"
    )


def format_thread(thread: list[dict]) -> str:
    root = thread[0]
    lines = [
        f"Thread anchored at `{root['path']}` line "
        f"{root.get('line') or root.get('original_line')}.\n",
        f"Diff hunk at the time of the original comment:\n```diff\n{root.get('diff_hunk', '')}\n```\n",
    ]
    for c in thread:
        author = c["user"]["login"]
        marker = " (this is you, the reviewer)" if author.endswith("[bot]") else ""
        lines.append(f"--- {author}{marker} at {c['created_at']}:\n{c['body']}\n")
    return "\n".join(lines)


def run_review(
    repo_root: str, diff: str, pr: dict, commits: list[dict], model: str = DEFAULT_MODEL
) -> dict:
    with tempfile.TemporaryDirectory(prefix="commit-checker-ctx-") as context_dir:
        paths = write_context_files(
            context_dir,
            {"pr.md": format_pr_context(pr, commits), "diff.patch": diff},
        )
        return run_claude(
            build_review_prompt(pr, commits, paths),
            REVIEW_SYSTEM_PROMPT,
            REVIEW_SCHEMA,
            repo_root,
            context_dir,
            model,
            review_turn_budget(diff),
        )


def run_responder(
    repo_root: str,
    diff: str,
    pr: dict,
    thread: list[dict],
    trigger: dict,
    model: str = DEFAULT_MODEL,
) -> tuple[str, dict]:
    with tempfile.TemporaryDirectory(prefix="commit-checker-ctx-") as context_dir:
        paths = write_context_files(
            context_dir,
            {"thread.md": format_thread(thread), "diff.patch": diff},
        )
        result = run_claude(
            build_responder_prompt(pr, trigger, paths),
            RESPONDER_SYSTEM_PROMPT,
            RESPONDER_SCHEMA,
            repo_root,
            context_dir,
            model,
            max_turns=20,
        )
    action = result.get("action", "no_response")
    return action, result
