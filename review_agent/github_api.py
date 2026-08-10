"""Minimal GitHub REST client for pull request review workflows."""

from __future__ import annotations

import logging

import requests

logger = logging.getLogger(__name__)

API_ROOT = "https://api.github.com"


class GitHubClient:
    def __init__(self, repo: str, token: str):
        # repo is "owner/name", e.g. from the GITHUB_REPOSITORY env var
        self.repo = repo
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            }
        )

    def _url(self, path: str) -> str:
        return f"{API_ROOT}/repos/{self.repo}{path}"

    def get_pr(self, number: int) -> dict:
        resp = self.session.get(self._url(f"/pulls/{number}"))
        resp.raise_for_status()
        return resp.json()

    def list_commits(self, number: int) -> list[dict]:
        commits: list[dict] = []
        page = 1
        while True:
            resp = self.session.get(
                self._url(f"/pulls/{number}/commits"),
                params={"per_page": 100, "page": page},
            )
            resp.raise_for_status()
            batch = resp.json()
            commits.extend(batch)
            if len(batch) < 100:
                return commits
            page += 1

    def get_diff(self, number: int) -> str:
        resp = self.session.get(
            self._url(f"/pulls/{number}"),
            headers={"Accept": "application/vnd.github.v3.diff"},
        )
        resp.raise_for_status()
        return resp.text

    def list_review_comments(self, number: int) -> list[dict]:
        comments: list[dict] = []
        page = 1
        while True:
            resp = self.session.get(
                self._url(f"/pulls/{number}/comments"),
                params={"per_page": 100, "page": page},
            )
            resp.raise_for_status()
            batch = resp.json()
            comments.extend(batch)
            if len(batch) < 100:
                return comments
            page += 1

    def reply_to_comment(self, number: int, comment_id: int, body: str) -> dict:
        resp = self.session.post(
            self._url(f"/pulls/{number}/comments/{comment_id}/replies"),
            json={"body": body},
        )
        resp.raise_for_status()
        return resp.json()

    def create_review(
        self, number: int, event: str, body: str, comments: list[dict]
    ) -> dict:
        """Submit a review. If inline comments are rejected (e.g. a line fell
        outside the diff), retry with the comments folded into the body so the
        review is never silently dropped."""
        payload: dict = {"event": event, "body": body}
        if comments:
            payload["comments"] = comments
        resp = self.session.post(self._url(f"/pulls/{number}/reviews"), json=payload)
        if resp.status_code == 422 and comments:
            logger.warning(
                "Inline comments rejected by GitHub (%s); retrying as body-only review",
                resp.text,
            )
            folded = body + "\n\n---\n### Additional findings\n" + "\n".join(
                f"- `{c['path']}:{c.get('line', '?')}` — {c['body']}" for c in comments
            )
            resp = self.session.post(
                self._url(f"/pulls/{number}/reviews"),
                json={"event": event, "body": folded},
            )
        resp.raise_for_status()
        return resp.json()
