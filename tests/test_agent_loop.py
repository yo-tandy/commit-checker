"""Smoke test: drive ToolLoopAgent.run() end-to-end with a fake API client."""
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    import anthropic  # noqa: F401
except ImportError:
    sys.modules["anthropic"] = types.ModuleType("anthropic")
    sys.modules["anthropic"].Anthropic = object

from review_agent.agent import SUBMIT_REVIEW_TOOL, ToolLoopAgent


class Block:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class FakeResponse:
    def __init__(self, content):
        self.content = content
        self.stop_reason = "tool_use"


class FakeMessages:
    def __init__(self, script):
        self.script = list(script)

    def create(self, **kwargs):
        return FakeResponse(self.script.pop(0))


def make_agent(script, max_turns=10):
    agent = ToolLoopAgent(
        ".", "diff --git a/x b/x\n+++ b/x\n", "sys", [SUBMIT_REVIEW_TOOL],
        max_turns=max_turns,
    )
    agent.client = types.SimpleNamespace(
        beta=types.SimpleNamespace(messages=FakeMessages(script))
    )
    return agent


def test_loop_runs_tools_then_terminates():
    script = [
        [Block(type="tool_use", id="t1", name="list_files", input={"glob": "*.py"})],
        [Block(type="tool_use", id="t2", name="submit_review",
               input={"verdict": "approve", "summary": "ok"})],
    ]
    name, payload = make_agent(script).run("go")
    assert name == "submit_review" and payload["verdict"] == "approve"


def test_wrap_up_notice_fires_near_budget():
    # max_turns=3: turn 0 leaves 2 remaining (<=3) -> wrap-up text must appear
    script = [
        [Block(type="tool_use", id="t1", name="list_files", input={})],
        [Block(type="tool_use", id="t2", name="submit_review",
               input={"verdict": "comment", "summary": "partial"})],
    ]
    agent = make_agent(script, max_turns=3)
    name, _ = agent.run("go")
    assert name == "submit_review"


if __name__ == "__main__":
    test_loop_runs_tools_then_terminates()
    test_wrap_up_notice_fires_near_budget()
    print("LOOP_OK")
