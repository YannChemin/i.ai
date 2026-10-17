"""Tests of the agents with a scripted backend (no language model is called)."""

import pytest

from iai_llm import Backend


class ScriptedBackend(Backend):
    """Return prepared replies in order and record the prompts."""

    name = "scripted"

    def __init__(self, replies):
        super().__init__()
        self.replies = list(replies)
        self.prompts = []

    def complete_json(self, system, messages, schema):
        self.prompts.append(messages[-1]["content"])
        return self.replies.pop(0)


@pytest.fixture
def make_copilot(parts):
    from iai_agents import Copilot
    from iai_context import ContextManager
    from iai_rag import ManualIndex
    from iai_tools import ToolRegistry

    catalog, registry, matcher, validator = parts

    def build(replies):
        backend = ScriptedBackend(replies)
        context = ContextManager(catalog, registry)
        tools = ToolRegistry(catalog, registry, matcher, validator, ManualIndex())
        return Copilot(backend, context, tools, validator, max_steps=6), backend

    return build


def test_execute_route_returns_valid_plan(make_copilot):
    copilot, backend = make_copilot(
        [
            {"intent": "execute", "confidence": 0.9},
            {
                "action": "tool",
                "tool": "module_help",
                "args": {"module": "r.slope.aspect"},
            },
            {
                "action": "plan",
                "answer": "Slope from elevation.",
                "plan": [
                    {
                        "module": "r.slope.aspect",
                        "params": {"elevation": "elevation", "slope": "slope_agent"},
                    }
                ],
            },
        ]
    )
    result = copilot.run("compute slope")
    assert result["kind"] == "plan"
    assert result["plan"][0].command.shell() == (
        "r.slope.aspect elevation=elevation slope=slope_agent"
    )
    assert "r.slope.aspect:" in backend.prompts[2]  # The interface was returned.
    assert copilot.context.last_plan[0]["module"] == "r.slope.aspect"


def test_invalid_plan_goes_back_to_the_agent(make_copilot):
    copilot, backend = make_copilot(
        [
            {"intent": "execute", "confidence": 0.9},
            {
                "action": "plan",
                "plan": [{"module": "r.info", "params": {"map": "elevaton"}}],
            },
            {
                "action": "plan",
                "plan": [{"module": "r.info", "params": {"map": "elevation"}}],
            },
        ]
    )
    result = copilot.run("show info of the elevaton map")
    assert result["kind"] == "plan"
    assert "does not exist" in backend.prompts[-1]


def test_query_route_runs_read_only_tool(make_copilot):
    copilot, backend = make_copilot(
        [
            {"intent": "query", "confidence": 0.95},
            {
                "action": "tool",
                "tool": "run_readonly",
                "args": {
                    "module": "r.univar",
                    "params": {"map": "elevation"},
                    "flags": "g",
                },
            },
            {"action": "answer", "answer": "The mean is 11."},
        ]
    )
    result = copilot.run("mean elevation?")
    assert result == {**result, "kind": "answer", "text": "The mean is 11."}
    assert "mean=11" in backend.prompts[-1]


def test_read_only_tool_refuses_writes(make_copilot):
    copilot, backend = make_copilot(
        [
            {"intent": "query", "confidence": 0.95},
            {
                "action": "tool",
                "tool": "run_readonly",
                "args": {
                    "module": "g.remove",
                    "params": {"type": "raster", "name": "elevation"},
                    "flags": "f",
                },
            },
            {"action": "answer", "answer": "Not allowed."},
        ]
    )
    copilot.run("delete elevation")
    assert "Refused" in backend.prompts[-1]


def test_low_confidence_asks_for_clarification(make_copilot):
    copilot, _ = make_copilot(
        [
            {"intent": "execute", "confidence": 0.2, "reason": "which map?"},
            {"questions": ["Which map?"], "suggestions": ["slope of elevation"]},
        ]
    )
    result = copilot.run("do the thing")
    assert result["kind"] == "clarify"
    assert result["questions"] == ["Which map?"]
