"""Entry point: gather PR context, run the review agent, post the review."""

from __future__ import annotations

import json
import logging
import os
import sys

from .agent import DEFAULT_MODEL, ReviewAgent, build_initial_prompt
from .diff_utils import commentable_lines
from .github_api import GitHubClient

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("review_agent")

VERDICT_TO_EVENT = {
    "approve": "APPROVE",
    "request_changes": "REQUEST_CHANGES",
    "comment": "COMMENT",
}


def resolve_pr_number() -> int:
    explicit = os.environ.get("PR_NUMBER", "").strip()
    if explicit:
        return int(explicit)
    event_path = os.environ.get("GITHUB_EVENT_PATH")
    if event_path and os.path.exists(event_path):
        with open(event_path) as f:
            event = json.load(f)
        if "pull_request" in event:
            return int(event["pull_request"]["number"])
        if "issue" in event and "pull_request" in event["issue"]:
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


def main() -> int:
    repo = os.environ["GITHUB_REPOSITORY"]
    token = os.environ["GITHUB_TOKEN"]
    repo_root = os.environ.get("GITHUB_WORKSPACE", os.getcwd())
    model = os.environ.get("REVIEW_MODEL", DEFAULT_MODEL)
    pr_number = resolve_pr_number()

    gh = GitHubClient(repo, token)
    pr = gh.get_pr(pr_number)
    commits = gh.list_commits(pr_number)
    diff = gh.get_diff(pr_number)

    if not diff.strip():
        logger.info("PR #%s has an empty diff; nothing to review.", pr_number)
        return 0

    logger.info(
        "Reviewing %s#%s (%d commits, %d diff bytes) with %s",
        repo, pr_number, len(commits), len(diff), model,
    )

    agent = ReviewAgent(repo_root, diff, model=model)
    review = agent.run(build_initial_prompt(pr, commits, diff))

    verdict = review.get("verdict", "comment")
    event = VERDICT_TO_EVENT.get(verdict, "COMMENT")
    summary = review.get("summary", "").strip() or "(no summary provided)"
    good, bad = validate_comments(review.get("comments") or [], commentable_lines(diff))
    if bad:
        summary += "\n\n---\n### Findings outside the diff\n" + "\n".join(
            f"- `{c.get('path', '?')}:{c.get('line', '?')}` — {c.get('body', '')}"
            for c in bad
        )

    body = f"## Automated review — verdict: **{verdict.replace('_', ' ')}**\n\n{summary}"
    posted = gh.create_review(pr_number, event, body, good)
    logger.info(
        "Posted %s review %s with %d inline comment(s): %s",
        event, posted.get("id"), len(good), posted.get("html_url", ""),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
