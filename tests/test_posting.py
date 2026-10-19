"""Posting logic: how a structured review result becomes a GitHub review."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import review_agent.__main__ as main  # noqa: E402

DIFF = "diff --git a/x.py b/x.py\n+++ b/x.py\n@@ -1 +1,2 @@\n line\n+new\n"

BOT_REVIEW = {
    "user": {"login": "github-actions[bot]"},
    "body": "earlier review",
    "state": "APPROVED",
}
NIT = {"path": "x.py", "line": 2, "body": "[non-blocking] nit"}


class FakeGH:
    def __init__(self, previous_reviews):
        self.repo = "o/r"
        self._previous = previous_reviews
        self.posted = None

    def get_pr(self, number):
        return {
            "number": number, "title": "t", "body": "",
            "base": {"ref": "main"}, "head": {"ref": "f"},
        }

    def list_commits(self, number):
        return [{"sha": "a" * 40, "commit": {"message": "feat: x"}}]

    def get_diff(self, number):
        return DIFF

    def list_reviews(self, number):
        return self._previous

    def create_review(self, number, event, body, comments):
        self.posted = {"event": event, "body": body, "comments": comments}
        return {"id": 1, "html_url": ""}


def post_review(previous, result):
    gh = FakeGH(previous)
    orig = main.run_review
    main.run_review = lambda *a, **k: result
    try:
        main.full_review(gh, {"pull_request": {"number": 5}}, ".", "m")
    finally:
        main.run_review = orig
    return gh.posted


def test_approving_rereview_folds_comments_into_summary():
    posted = post_review(
        [BOT_REVIEW], {"verdict": "approve", "summary": "ok", "comments": [NIT]}
    )
    assert posted["event"] == "APPROVE"
    assert posted["comments"] == []
    assert "Non-blocking notes" in posted["body"]
    assert "`x.py:2`" in posted["body"] and "[non-blocking] nit" in posted["body"]


def test_first_review_keeps_inline_comments():
    posted = post_review(
        [], {"verdict": "approve", "summary": "ok", "comments": [NIT]}
    )
    assert posted["event"] == "APPROVE"
    assert len(posted["comments"]) == 1
    assert "Non-blocking notes" not in posted["body"]


def test_rereview_requesting_changes_keeps_inline_comments():
    posted = post_review(
        [BOT_REVIEW],
        {"verdict": "request_changes", "summary": "bad", "comments": [NIT]},
    )
    assert posted["event"] == "REQUEST_CHANGES"
    assert len(posted["comments"]) == 1


def test_out_of_diff_comments_still_fold_on_first_review():
    off_diff = {"path": "x.py", "line": 99, "body": "outside"}
    posted = post_review(
        [], {"verdict": "approve", "summary": "ok", "comments": [off_diff]}
    )
    assert posted["comments"] == []
    assert "Findings outside the diff" in posted["body"] and "`x.py:99`" in posted["body"]


if __name__ == "__main__":
    test_approving_rereview_folds_comments_into_summary()
    test_first_review_keeps_inline_comments()
    test_rereview_requesting_changes_keeps_inline_comments()
    test_out_of_diff_comments_still_fold_on_first_review()
    print("POSTING_OK")
