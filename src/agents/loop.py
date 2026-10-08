"""Manual tool-use loop shared by both architectures.

A manual loop (rather than the SDK's beta tool runner) keeps every tool call
going through ``run_tool`` so each execution lands in the evidence ledger and is
counted in telemetry.
"""
from __future__ import annotations

import json
from typing import Any

from src.llm import LLMClient, LLMError
from src.tools import TOOLS, ToolContext, run_tool, tool_result_payload


def purchase_request_block(request: dict[str, Any]) -> str:
    return (
        "<purchase_request untrusted=\"true\">\n"
        f"{json.dumps(request, indent=2, default=str)}\n"
        "</purchase_request>"
    )


def run_tool_agent(
    llm: LLMClient,
    ctx: ToolContext,
    *,
    agent: str,
    system: str,
    user_content: str,
    tool_names: list[str],
    terminal_tool: dict[str, Any],
    max_turns: int,
) -> dict[str, Any]:
    """Run an agent until it calls ``terminal_tool``; return that tool's input.

    Raises ``LLMError`` if the model never submits within ``max_turns``.
    """
    tools = [TOOLS[n].to_api() for n in tool_names] + [terminal_tool]
    terminal_name = terminal_tool["name"]
    messages: list[dict[str, Any]] = [{"role": "user", "content": user_content}]
    nudged = False

    for _ in range(max_turns):
        response = llm.create(agent=agent, system=system, messages=messages, tools=tools)
        messages.append({"role": "assistant", "content": response.content})
        tool_uses = [b for b in response.content if getattr(b, "type", None) == "tool_use"]

        if not tool_uses:
            if nudged:
                break
            nudged = True
            messages.append({"role": "user", "content": f"Call {terminal_name} now with your structured result."})
            continue

        submitted: dict[str, Any] | None = None
        results: list[dict[str, Any]] = []
        for block in tool_uses:
            if block.name == terminal_name:
                submitted = dict(block.input)
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": "Received."})
                continue
            if block.name not in tool_names:
                results.append({"type": "tool_result", "tool_use_id": block.id, "is_error": True,
                                "content": f"Tool '{block.name}' is not available to this agent."})
                continue
            entry = run_tool(ctx, block.name, dict(block.input), caller=agent)
            results.append({"type": "tool_result", "tool_use_id": block.id, "content": tool_result_payload(entry),
                            "is_error": entry.output.get("status") == "error"})
        if submitted is not None:
            return submitted
        messages.append({"role": "user", "content": results})

    raise LLMError(f"{agent} did not call {terminal_name} within {max_turns} turns")
