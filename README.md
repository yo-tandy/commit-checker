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

Powered by **Claude Code (CLI)** running headless in the workflow: it supplies
the agent loop and its own Read/Grep/Glob/Bash tools (restricted to read-only
plus scoped `git` commands), while a thin Python wrapper prepares PR context,
enforces structured output, validates comment anchors, and posts the review.

**Auth — two options** (provide exactly one):
- `anthropic_api_key` — a metered Anthropic API key, or
- `claude_code_oauth_token` — a token from running `claude setup-token`
  locally, which bills your Claude subscription instead of an API key.

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
| `anthropic_api_key` | one of the two | — | Anthropic API key (metered) |
| `claude_code_oauth_token` | one of the two | — | Token from `claude setup-token` (subscription billing) |
| `github_token` | yes | — | Token with `pull-requests: write` |
| `pr_number` | no | from event | Review a specific PR (useful for `workflow_dispatch`) |
| `model` | no | `claude-opus-5` | Model alias or full ID (`opus`, `claude-opus-5`, …) |
| `max_budget_usd` | no | — | Per-run spend cap (passed to `--max-budget-usd`) |

## How it works

```
gather PR (title, body, commits, diff) ──► context files (pr.md, diff.patch)
        │
        ▼
claude --bare -p ... --json-schema <review schema>
   --permission-mode dontAsk
   --allowedTools "Read,Grep,Glob,Bash(git diff *),..."   (read-only)
   --max-turns <scaled with diff size>
        │
        ▼
structured review {verdict, summary, comments}
        │
        ▼
validate comment anchors against the diff ──► POST /pulls/{n}/reviews
                                              (APPROVE / REQUEST_CHANGES / COMMENT)
```

Details worth knowing:

- **Claude Code runs `--bare`**: hooks, skills, MCP servers, and CLAUDE.md
  files in the reviewed repo are *not* loaded, so a PR can't inject
  instructions into its own reviewer through those channels.
- **Read-only by construction**: no Edit/Write tools, Bash limited to scoped
  `git`/`ls` commands, and `--permission-mode dontAsk` auto-denies everything
  else.
- **Turn budget scales with PR size** (`30 + 3/file`, capped at 120), with an
  optional `max_budget_usd` spend cap on top.
- **Comment validation**: GitHub rejects inline comments on lines outside the
  diff, so proposed anchors are validated first; anything unanchorable is
  folded into the review body instead of being dropped.
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
pip install -r requirements.txt   # requests only
# claude CLI must be installed and authenticated (`claude` login, or export
# ANTHROPIC_API_KEY / CLAUDE_CODE_OAUTH_TOKEN)
export ANTHROPIC_API_KEY=...
export GITHUB_TOKEN=...           # repo-scoped token
export GITHUB_REPOSITORY=owner/name
export PR_NUMBER=123
export GITHUB_WORKSPACE=/path/to/checkout-of-that-repo   # PR head checked out
python -m review_agent
```

## Cost & safety notes

- Each review is a multi-turn agentic run; expect roughly a few cents to a few
  tens of cents per PR on an API key, or subscription usage with an OAuth
  token. Set `max_budget_usd` for a hard per-run cap.
- The reviewer's tools are read-only; it runs `--bare`, so repo-local
  configuration can't influence it.
- Treat the agent's approval as one signal, not a replacement for required
  human review on sensitive repositories — branch protection rules remain
  yours to configure.
