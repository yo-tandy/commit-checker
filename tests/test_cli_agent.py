"""Smoke tests: drive the Claude Code invocation with a fake `claude` binary."""

import json
import os
import stat
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from review_agent.agent import (  # noqa: E402
    _extract_structured,
    run_responder,
    run_review,
    review_turn_budget,
)

PR = {"number": 7, "title": "t", "base": {"ref": "main"}, "head": {"ref": "f"}}
COMMITS = [{"sha": "a" * 40, "commit": {"message": "feat: x"}}]
DIFF = "diff --git a/x.py b/x.py\n+++ b/x.py\n@@ -1 +1,2 @@\n line\n+new\n"


def make_fake_claude(tmpdir: str, structured: dict) -> str:
    """Create a fake `claude` that logs argv and emits canned JSON output."""
    args_log = os.path.join(tmpdir, "args.json")
    payload = json.dumps(
        {"result": "ok", "structured_output": structured, "total_cost_usd": 0.01}
    )
    script = os.path.join(tmpdir, "claude")
    with open(script, "w") as f:
        f.write(
            "#!/usr/bin/env python3\n"
            "import json, sys, os\n"
            f"json.dump({{'argv': sys.argv[1:], 'cwd': os.getcwd()}}, open({args_log!r}, 'w'))\n"
            f"print({payload!r})\n"
        )
    os.chmod(script, os.stat(script).st_mode | stat.S_IEXEC)
    return args_log


def test_run_review_invokes_cli_and_returns_structured():
    with tempfile.TemporaryDirectory() as tmp:
        args_log = make_fake_claude(
            tmp, {"verdict": "approve", "summary": "ok", "comments": []}
        )
        os.environ["CLAUDE_BIN"] = os.path.join(tmp, "claude")
        try:
            review = run_review(tmp, DIFF, PR, COMMITS, model="claude-opus-5")
        finally:
            del os.environ["CLAUDE_BIN"]
        assert review["verdict"] == "approve"

        argv = json.load(open(args_log))
        flags = argv["argv"]
        assert "--bare" in flags and "-p" in flags
        assert "--json-schema" in flags and "--permission-mode" in flags
        assert flags[flags.index("--model") + 1] == "claude-opus-5"
        assert flags[flags.index("--max-turns") + 1] == str(review_turn_budget(DIFF))
        assert argv["cwd"] == os.path.realpath(tmp)  # runs in the repo root
        # no write tools allowed
        allowed = flags[flags.index("--allowedTools") + 1]
        assert "Edit" not in allowed and "Write" not in allowed


def test_run_responder_maps_action():
    thread = [
        {"path": "x.py", "line": 2, "diff_hunk": "@@", "body": "issue",
         "user": {"login": "github-actions[bot]"}, "created_at": "t0"},
        {"body": "fixed", "user": {"login": "human"}, "created_at": "t1"},
    ]
    with tempfile.TemporaryDirectory() as tmp:
        make_fake_claude(tmp, {"action": "post_reply", "body": "Verified."})
        os.environ["CLAUDE_BIN"] = os.path.join(tmp, "claude")
        try:
            action, payload = run_responder(tmp, DIFF, PR, thread, thread[-1])
        finally:
            del os.environ["CLAUDE_BIN"]
        assert action == "post_reply" and payload["body"] == "Verified."


def test_extract_structured_fallbacks():
    direct = json.dumps({"result": "x", "structured_output": {"a": 1}})
    assert _extract_structured(direct) == {"a": 1}
    fenced = json.dumps({"result": "```json\n{\"a\": 2}\n```"})
    assert _extract_structured(fenced) == {"a": 2}
    bare = json.dumps({"result": "{\"a\": 3}"})
    assert _extract_structured(bare) == {"a": 3}
    err = json.dumps({"result": "boom", "is_error": True})
    try:
        _extract_structured(err)
        raise AssertionError("expected RuntimeError")
    except RuntimeError:
        pass


if __name__ == "__main__":
    test_run_review_invokes_cli_and_returns_structured()
    test_run_responder_maps_action()
    test_extract_structured_fallbacks()
    print("CLI_AGENT_OK")
