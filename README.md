# commit-checker — Claude PR Review Agent

A reusable GitHub Action that reviews pull requests with Claude. On every PR it:

- reads the **commit history** of the PR and judges commit quality and messages,
- checks whether **documentation** was updated where behavior changed,
- evaluates **test coverage and quality** for the changed behavior,
- hunts for **security risks** (injection, secrets, authz gaps, unsafe patterns),
- explores the **whole checked-out codebase** with list/read/search tools — not just the diff,
- posts a GitHub review with **inline comments** anchored to the diff,
- **approves, requests changes, or comments** based on what it found, and
- **responds to replies** on its own comment threads — verifying claimed
  fixes against the code, answering questions, and conceding when it's wrong.

Powered by `claude-opus-5` via the official Anthropic Python SDK, with adaptive
thinking and server-side refusal fallbacks enabled by default.

## Setup

1. Push this repository to GitHub (e.g. `yourname/commit-checker`).
2. In each repo you want reviewed (or once at the org level):
   - Add an `ANTHROPIC_API_KEY` Actions secret.
   - Copy [`example-workflow.yml`](example-workflow.yml) to
     `.github/workflows/claude-review.yml`, replacing `OWNER` with your
     username/org.
3. To let the agent **approve** PRs with the default `GITHUB_TOKEN`, enable
   *Settings → Actions → General → "Allow GitHub Actions to create and approve
   pull requests"*. Without it, `REQUEST_CHANGES` and `COMMENT` still work, but
   `APPROVE` is rejected by GitHub. Alternatively pass a PAT (or a GitHub App
   token) as `github_token` — reviews then appear under that identity, and an
   approval from it can satisfy branch-protection required reviews.

## Inputs

| Input | Required | Default | Description |
|---|---|---|---|
| `anthropic_api_key` | yes | — | Anthropic API key |
| `github_token` | yes | — | Token with `pull-requests: write` |
| `pr_number` | no | from event | Review a specific PR (useful for `workflow_dispatch`) |
| `model` | no | `claude-opus-5` | Claude model ID |

## How it works

```
gather PR (title, body, commits, diff)
        │
        ▼
Claude agent loop ──── tools: list_files / read_file / search / get_file_diff
        │                     (sandboxed to the repo root, PR head checkout)
        ▼
submit_review(verdict, summary, inline comments)
        │
        ▼
validate comment anchors against the diff ──► POST /pulls/{n}/reviews
                                              (APPROVE / REQUEST_CHANGES / COMMENT)
```

Details worth knowing:

- **Large diffs**: if the diff exceeds ~60 KB it isn't inlined; the agent sees
  the changed-file list and pulls per-file diffs on demand.
- **Comment validation**: GitHub rejects inline comments on lines outside the
  diff, so proposed anchors are validated first; anything unanchorable is
  folded into the review body instead of being dropped.
- **Refusals**: the request opts into Anthropic's server-side fallback chain
  (`fallbacks: "default"`), so a safety-classifier decline is retried on the
  recommended fallback model automatically.
- **Verdict policy** (tunable in `review_agent/agent.py`):
  `request_changes` for probable bugs, security risks, committed secrets, or
  substantive untested behavior; `approve` when the change is solid;
  `comment` otherwise.

## Responding to comment replies

When someone replies in a thread rooted at one of the agent's inline comments
(identified by an invisible `<!-- commit-checker -->` marker), the
`pull_request_review_comment` trigger runs the agent in responder mode. It
reads the whole thread, re-checks the current code, and either replies
in-thread or deliberately stays silent:

- "Fixed in abc123" → verifies the fix in the checkout; confirms, or points
  out what's still missing (or notes the commit isn't pushed yet).
- Questions → answered from the actual code.
- Disagreement → re-examined; it concedes when wrong, and pushes back at most
  once when the finding stands.
- Bare acknowledgments ("ack", "will do") → no reply.

Loop safety: comments from `*[bot]` accounts are ignored, and GitHub doesn't
trigger workflows from events created by the default `GITHUB_TOKEN`, so the
agent can't converse with itself.

Known limitation: clicking **"Resolve conversation"** emits no workflow event
(a GitHub platform gap), so the agent can't react to resolutions directly.
Pushing new commits triggers a fresh full review, which covers the same
ground.

## Running locally

```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY=...      # or `ant auth login`
export GITHUB_TOKEN=...           # repo-scoped token
export GITHUB_REPOSITORY=owner/name
export PR_NUMBER=123
export GITHUB_WORKSPACE=/path/to/checkout-of-that-repo   # PR head checked out
python -m review_agent
```

## Cost & safety notes

- Each review is a multi-turn agentic run on Opus 5; expect roughly a few cents
  to a few tens of cents per PR depending on diff size and repo exploration.
- The agent's file tools are read-only and confined to the repository root.
- Treat the agent's approval as one signal, not a replacement for required
  human review on sensitive repositories — branch protection rules remain
  yours to configure.
