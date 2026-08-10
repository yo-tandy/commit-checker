"""Entry point. Dispatches on the triggering GitHub event:

- pull_request events        -> full review (verdict + inline comments)
- pull_request_review_comment -> respond (or not) to a reply on one of our threads
"""

from __future__ import annotations

import json
import logging
import os
import sys

from .agent import DEFAULT_MODEL, run_responder, run_review
from .diff_utils import commentable_lines
from .github_api import GitHubClient

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("review_agent")

# Invisible tag appended to every comment we post; identifies our threads.
MARKER = "<!-- commit-checker -->"

VERDICT_TO_EVENT = {
    "approve": "APPROVE",
    "request_changes": "REQUEST_CHANGES",
    "comment": "COMMENT",
}


def load_event() -> dict:
    event_path = os.environ.get("GITHUB_EVENT_PATH")
    if event_path and os.path.exists(event_path):
        with open(event_path) as f:
            return json.load(f)
    return {}


def resolve_pr_number(event: dict) -> int:
    explicit = os.environ.get("PR_NUMBER", "").strip()
    if explicit:
        return int(explicit)
    if "pull_request" in event:
        return int(event["pull_request"]["number"])
    if "issue" in event and "pull_request" in event.get("issue", {}):
        return int(event["issue"]["number"])
    raise SystemExit(
        "Could not determine the PR number: set PR_NUMBER or run on a pull_request event."
    )


def validate_comments(
    comments: list[dict], valid_lines: dict[str, set[int]]
) -> tuple[list[dict], list[dict]]:
    """Split proposed inline comments into postable and out-of-diff."""
    good, bad = [], []
    for c in comments:
        path, line = c.get("path"), c.get("line")
        if path in valid_lines and isinstance(line, int) and line in valid_lines[path]:
            good.append({"path": path, "line": line, "side": "RIGHT", "body": c["body"]})
        else:
            bad.append(c)
    return good, bad


def full_review(gh: GitHubClient, event: dict, repo_root: str, model: str) -> int:
    pr_number = resolve_pr_number(event)
    pr = gh.get_pr(pr_number)
    commits = gh.list_commits(pr_number)
    diff = gh.get_diff(pr_number)

    if not diff.strip():
        logger.info("PR #%s has an empty diff; nothing to review.", pr_number)
        return 0

    logger.info(
        "Reviewing %s#%s (%d commits, %d diff bytes) with %s",
        gh.repo, pr_number, len(commits), len(diff), model,
    )

    review = run_review(repo_root, diff, pr, commits, model=model)

    verdict = review.get("verdict", "comment")
    gh_event = VERDICT_TO_EVENT.get(verdict, "COMMENT")
    summary = review.get("summary", "").strip() or "(no summary provided)"
    good, bad = validate_comments(review.get("comments") or [], commentable_lines(diff))
    for c in good:
        c["body"] = c["body"] + "\n\n" + MARKER
    if bad:
        summary += "\n\n---\n### Findings outside the diff\n" + "\n".join(
            f"- `{c.get('path', '?')}:{c.get('line', '?')}` — {c.get('body', '')}"
            for c in bad
        )

    body = f"## Automated review — verdict: **{verdict.replace('_', ' ')}**\n\n{summary}"
    posted = gh.create_review(pr_number, gh_event, body, good)
    logger.info(
        "Posted %s review %s with %d inline comment(s): %s",
        gh_event, posted.get("id"), len(good), posted.get("html_url", ""),
    )
    return 0


def respond_to_comment(gh: GitHubClient, event: dict, repo_root: str, model: str) -> int:
    comment = event.get("comment") or {}
    pr = event.get("pull_request") or {}
    if not comment or not pr:
        logger.info("Event carries no review comment; nothing to do.")
        return 0

    author = comment.get("user", {}).get("login", "")
    if author.endswith("[bot]"):
        logger.info("Comment author %s is a bot; skipping.", author)
        return 0

    root_id = comment.get("in_reply_to_id")
    if not root_id:
        logger.info("Top-level comment (not a reply to a review thread); skipping.")
        return 0

    pr_number = int(pr["number"])
    all_comments = gh.list_review_comments(pr_number)
    root = next((c for c in all_comments if c["id"] == root_id), None)
    if root is None or MARKER not in (root.get("body") or ""):
        logger.info("Thread root is not a commit-checker comment; skipping.")
        return 0

    thread = sorted(
        (
            c
            for c in all_comments
            if c["id"] == root_id or c.get("in_reply_to_id") == root_id
        ),
        key=lambda c: c["created_at"],
    )
    diff = gh.get_diff(pr_number)

    logger.info(
        "Reply from %s on thread %s (%s); deciding whether to respond.",
        author, root_id, root.get("path"),
    )
    action, payload = run_responder(repo_root, diff, pr, thread, comment, model=model)

    if action == "no_response":
        logger.info("No response needed: %s", payload.get("reason", ""))
        return 0

    body = payload.get("body", "").strip()
    if not body:
        logger.info("Empty reply body; skipping.")
        return 0
    posted = gh.reply_to_comment(pr_number, root_id, body + "\n\n" + MARKER)
    logger.info("Posted reply %s: %s", posted.get("id"), posted.get("html_url", ""))
    return 0


def main() -> int:
    repo = os.environ["GITHUB_REPOSITORY"]
    token = os.environ["GITHUB_TOKEN"]
    repo_root = os.environ.get("GITHUB_WORKSPACE", os.getcwd())
    model = os.environ.get("REVIEW_MODEL", DEFAULT_MODEL)
    event = load_event()
    event_name = os.environ.get("GITHUB_EVENT_NAME", "pull_request")

    gh = GitHubClient(repo, token)
    if event_name == "pull_request_review_comment":
        return respond_to_comment(gh, event, repo_root, model)
    return full_review(gh, event, repo_root, model)


if __name__ == "__main__":
    sys.exit(main())
