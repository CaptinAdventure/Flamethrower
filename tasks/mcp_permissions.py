import re


_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,199}", re.ASCII)


def valid_mcp_kind(value):
    return isinstance(value, str) and bool(_NAME.fullmatch(value))


def reported_mcp_kind_mismatch(tool_call):
    origin = tool_call.get("originToolCall")
    if not isinstance(origin, dict):
        return None
    initial, current = origin.get("kind"), tool_call.get("kind")
    intermediate = origin.get("nonReadKind")
    if not valid_mcp_kind(initial) or not valid_mcp_kind(current):
        return None
    if "nonReadKind" in origin and not valid_mcp_kind(intermediate):
        return None
    if initial != current or intermediate is not None:
        mismatch = {"origin": initial, "permission": current}
        if intermediate is not None:
            mismatch["intermediate"] = intermediate
        return mismatch
    return None


def reported_mcp_tool(tool_call):
    origin = tool_call.get("originToolCall")
    if not isinstance(origin, dict):
        return None
    title = tool_call.get("title")
    qualified = origin.get("title")
    raw = tool_call.get("rawInput")
    if (
        not isinstance(title, str) or not _NAME.fullmatch(title)
        or not isinstance(qualified, str) or not isinstance(raw, dict)
        or origin.get("rawInput") != raw or not qualified.endswith("-" + title)
        or not valid_mcp_kind(origin.get("kind")) or not valid_mcp_kind(tool_call.get("kind"))
        or (origin["kind"] != tool_call.get("kind") and not reported_mcp_kind_mismatch(tool_call))
        or ("nonReadKind" in origin and not reported_mcp_kind_mismatch(tool_call))
    ):
        return None
    server = qualified[:-(len(title) + 1)]
    if not _NAME.fullmatch(server):
        return None
    return {"server": server, "tool": title}


def matching_mcp_rule(tool_call, rules):
    identity = reported_mcp_tool(tool_call)
    if identity is None or not isinstance(rules, list):
        return None
    mismatch = reported_mcp_kind_mismatch(tool_call)
    for rule in rules:
        if (
            isinstance(rule, dict) and rule.get("server") == identity["server"]
            and rule.get("kindMismatch") == mismatch
            and (rule.get("tool") == identity["tool"] or ("tool" in rule and rule["tool"] is None))
        ):
            return rule
    return None
