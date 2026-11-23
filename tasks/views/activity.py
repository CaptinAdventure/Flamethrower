import json

from ..mcp_permissions import reported_mcp_tool

ACTIVITY_HEADINGS = {
    "message": "Message",
    "tool": "Tool activity",
    "permission decision": "Permission decision",
}


def tool_call_summary(tool_call):
    kind = tool_call.get("kind")
    if not isinstance(kind, str):
        kind = ""
    raw = tool_call.get("rawInput")
    labels = {
        "execute": "Copilot wants to run a command",
        "edit": "Copilot wants to edit files",
        "delete": "Copilot wants to delete files",
        "move": "Copilot wants to move files",
        "read": "Copilot wants to read files",
        "search": "Copilot wants to search",
        "fetch": "Copilot wants to access a network resource",
    }
    if kind in ("read", "search"):
        category, category_class = "Read / search", "read"
    elif kind in ("edit", "delete", "move"):
        category, category_class = "Write / change", "write"
    elif kind == "execute":
        category, category_class = "Command — may read or write", "write"
    elif kind == "fetch":
        category, category_class = "Network access", "other"
    else:
        category, category_class = "Other action — effects not verified", "other"
    summary = {
        "heading": labels.get(kind, "Copilot requests a tool action"),
        "category": category,
        "category_class": category_class,
        "title": tool_call.get("title", "") if isinstance(tool_call.get("title"), str) else "",
        "status": tool_call.get("status", "") if isinstance(tool_call.get("status"), str) else "",
        "command": "",
        "paths": [],
        "input_text": "",
        "output": [],
    }
    mcp_tool = reported_mcp_tool(tool_call)
    if mcp_tool:
        summary.update(
            heading="Copilot requests a reported MCP tool",
            category="MCP tool — effects not verified",
            category_class="other",
            mcp_tool=mcp_tool,
            input_text=json.dumps(raw, indent=2, ensure_ascii=False),
        )
    if isinstance(raw, dict):
        if isinstance(raw.get("command"), str):
            summary["command"] = raw["command"]
        for key in ("diff", "text"):
            if not mcp_tool and isinstance(raw.get(key), str):
                summary["input_text"] = raw[key]
                break
    elif isinstance(raw, str):
        if kind == "execute":
            summary["command"] = raw
        else:
            summary["input_text"] = raw
    locations = tool_call.get("locations")
    if isinstance(locations, list):
        for location in locations:
            if isinstance(location, dict) and isinstance(location.get("path"), str):
                summary["paths"].append(location["path"])
    if isinstance(raw, dict):
        for key in ("fileName", "filePath", "file_path", "path", "projectPath", "pathInProject", "directoryPath", "cwd"):
            path = raw.get(key)
            if isinstance(path, str) and path not in summary["paths"]:
                summary["paths"].append(path)
    output = tool_call.get("rawOutput")
    if isinstance(output, str):
        summary["output"].append(output)
    elif isinstance(output, dict):
        for key in ("message", "text", "stdout", "stderr"):
            if isinstance(output.get(key), str) and output[key] not in summary["output"]:
                summary["output"].append(output[key])
    content = tool_call.get("content")
    if isinstance(content, list):
        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "diff" and isinstance(item.get("path"), str) and item["path"] not in summary["paths"]:
                summary["paths"].append(item["path"])
            block = item.get("content")
            if item.get("type") == "content" and isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text")
                if isinstance(text, str) and text not in summary["output"]:
                    summary["output"].append(text)
    return summary

def format_activity(events):
    formatted = []
    tool_calls = {}
    for event in events:
        if event.kind == "message" and formatted and formatted[-1]["kind"] == "message":
            formatted[-1]["content"] += event.content
            continue
        item = {
            "kind": event.kind,
            "heading": ACTIVITY_HEADINGS.get(event.kind, event.kind.replace("_", " ").capitalize()),
            "content": event.content,
            "created_at": event.created_at,
        }
        if event.kind in ("tool", "permission decision"):
            try:
                data = json.loads(event.content)
            except json.JSONDecodeError:
                data = None
            if event.kind == "permission decision" and isinstance(data, dict):
                tool_call = data.get("toolCall")
                option = data.get("selectedOption")
                if isinstance(tool_call, dict) and isinstance(option, dict):
                    item["summary"] = tool_call_summary(tool_call)
                    item["automatic"] = data.get("automatic") is True
                    item["decision"] = (
                        f"Automatically approved: {option.get('name', '')}"
                        if item["automatic"] else option.get("name", "")
                    )
            elif event.kind == "tool" and isinstance(data, dict) and any(
                key in data for key in ("toolCallId", "title", "kind", "status", "rawInput", "rawOutput", "content", "locations")
            ):
                tool_id = data.get("toolCallId")
                if isinstance(tool_id, str) and tool_id:
                    previous = tool_calls.setdefault(tool_id, {})
                    previous.update({key: value for key, value in data.items() if value is not None})
                    data = previous
                item["summary"] = tool_call_summary(data)
            if "summary" not in item:
                item["unformatted"] = True
        formatted.append(item)
    return formatted
