"""The ReAct loop shared by both agents (New US jobs and Archive match); each agent supplies its own tools."""
from typing import NamedTuple

from .latest_rankers import usage_tokens

OLD_READ_CHARS = 200


class Step(NamedTuple):
    header: str                 # what the history shows after "Step N: "
    observation: str = ""
    args: dict | None = None
    error: str | None = None
    is_read: bool = False       # counts against the read budget; older reads are shortened in the history
    done: object = None         # a successful submit's value; ends the loop


def run_agent(client, system, static, tools, schema, max_steps, max_reads, max_tokens):
    """Run tools[action](action, reads_left) -> Step until one returns done.

    Returns (done, meta). meta holds calls, tokens, the trace and `fallback`: None after a successful submit,
    else why the loop stopped ("llm_error: …", "token_budget" or "max_steps") so the caller can fall back.
    """
    history, trace = [], []
    meta = {"calls": 0, "input_tokens": 0, "output_tokens": 0, "trace": trace, "fallback": None}
    reads = 0
    for step in range(1, max_steps + 1):
        rendered = []
        for number, (header, observation, is_read) in enumerate(history):
            if is_read and number < len(history) - 1 and len(observation) > OLD_READ_CHARS:
                observation = observation[:OLD_READ_CHARS] + " …(truncated)"
            rendered.append(f"{header}\nObservation: {observation}")
        prompt = (static + "\nHISTORY:\n" + ("\n\n".join(rendered) if rendered else "(none yet)")
                  + f"\n\nSteps left: {max_steps - step + 1}, reads left: {max(max_reads - reads, 0)}")
        if step == max_steps:
            prompt += "\nThis is your last step: submit now."
        try:
            action, metadata = client.generate(system, prompt, schema)
        except Exception as exc:  # any client failure (HTTP, network, bad JSON) ends the run; the caller falls back
            meta["fallback"] = f"llm_error: {type(exc).__name__}: {exc}"
            return None, meta
        meta["calls"] += 1
        used_in, used_out = usage_tokens(metadata)
        meta["input_tokens"] += used_in
        meta["output_tokens"] += used_out
        kind = action.get("action") if isinstance(action, dict) else None
        thought = str(action.get("thought") or "")[:500] if isinstance(action, dict) else ""
        tool = tools.get(kind)
        if tool is None:
            names = list(tools)
            message = "Unknown action. Use " + ", ".join(names[:-1]) + " or " + names[-1] + "."
            result = Step("invalid action", message, error=message)
        else:
            result = tool(action, max_reads - reads)
        trace.append({"step": step, "action": kind, "args": result.args or {}, "error": result.error,
                      "thought": thought})
        if result.done is not None:
            return result.done, meta
        reads += result.is_read
        header = f"Step {step}: {result.header}"
        history.append((f"Thought: {thought}\n{header}" if thought else header, result.observation, result.is_read))
        if meta["input_tokens"] + meta["output_tokens"] > max_tokens:
            meta["fallback"] = "token_budget"
            return None, meta
    meta["fallback"] = "max_steps"
    return None, meta
