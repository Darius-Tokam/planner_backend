import json

from pydantic_ai.messages import ModelMessagesTypeAdapter, ModelRequest, UserPromptPart


def bounded_history(raw, *, max_bytes=48000, max_turns=8):
    """Drop whole completed user turns, never a tool call without its result.

    Active deferred turns are supplied as an invocation's exact frozen history,
    and never pass through this compactor. This is selection, not a fake summary.
    """
    messages = ModelMessagesTypeAdapter.validate_python(raw)
    groups = []
    for message in messages:
        if isinstance(message, ModelRequest) and any(isinstance(p, UserPromptPart) for p in message.parts):
            groups.append([])
        if not groups:
            groups.append([])
        groups[-1].append(message)
    selected, size = [], 0
    for group in reversed(groups):
        cost = len(ModelMessagesTypeAdapter.dump_json(group))
        if len(selected) >= max_turns or size + cost > max_bytes:
            break
        selected.append(group)
        size += cost
    kept = [message for group in reversed(selected) for message in group]
    return json.loads(ModelMessagesTypeAdapter.dump_json(kept)), len(groups) - len(selected)
