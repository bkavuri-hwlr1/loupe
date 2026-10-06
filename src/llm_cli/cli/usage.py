"""Readable token counts for the status footer, /usage, and /compact."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from llm_cli.agent.limits import COMPACT_AT_FRACTION


def format_tokens(count: int) -> str:
    if count < 1_000:
        return str(count)
    if count < 10_000:
        return f"{count / 1_000:.1f}k"
    if count < 1_000_000:
        return f"{round(count / 1_000)}k"
    return f"{count / 1_000_000:.1f}M"


def context_percent(tokens: object, budget: object) -> int | None:
    """How full the context is, or None when either size is unknown."""

    if type(tokens) is not int or type(budget) is not int or tokens < 0 or budget <= 0:
        return None
    return round(100 * tokens / budget)


def usage_lines(result: Mapping[str, Any]) -> list[str]:
    """Describe a session.usage result in a few plain lines."""

    usage = result.get("usage")
    counts = usage if isinstance(usage, Mapping) else {}
    runs = result.get("task_runs")
    runs = runs if type(runs) is int else 0

    def count(key: str) -> int:
        value = counts.get(key)
        return value if type(value) is int and value >= 0 else 0

    lines: list[str] = []
    if runs == 0:
        lines.append("No token usage recorded in this conversation yet.")
    else:
        lines.append(
            f"Token usage in this conversation ({runs} task "
            f"{'run' if runs == 1 else 'runs'}):"
        )
        prompt = count("prompt_tokens")
        cached = count("cache_read_input_tokens")
        line = f"  Prompt:  {format_tokens(prompt)} tokens"
        if cached:
            line += f", {format_tokens(cached)} read from cache"
        lines.append(line)
        output = count("output_tokens")
        reasoning = count("reasoning_tokens")
        line = f"  Output:  {format_tokens(output)} tokens"
        if reasoning:
            line += f", {format_tokens(reasoning)} of them reasoning"
        lines.append(line)
        helper_prompt = count("explore_prompt_tokens")
        helper_output = count("explore_output_tokens")
        if helper_prompt or helper_output:
            lines.append(
                f"  Of these, explore helpers used {format_tokens(helper_prompt)} "
                f"prompt and {format_tokens(helper_output)} output tokens."
            )
    tokens = result.get("context_tokens")
    budget = result.get("context_budget")
    percent = context_percent(tokens, budget)
    if percent is not None:
        assert type(tokens) is int and type(budget) is int
        lines.append(
            f"  Context: {format_tokens(tokens)} of {format_tokens(budget)} tokens "
            f"({percent}%). Loupe summarizes the conversation at "
            f"{round(COMPACT_AT_FRACTION * 100)}%."
        )
    elif type(tokens) is int and tokens >= 0:
        lines.append(f"  Context: {format_tokens(tokens)} tokens.")
    lines.append(
        "  Loupe counts tokens, not cost: prices depend on your provider and plan."
    )
    return lines


__all__ = ["context_percent", "format_tokens", "usage_lines"]
