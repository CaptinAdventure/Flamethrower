import re


_COMMAND = re.compile(r"[A-Za-z0-9_./,:=@+\-]+(?:[ \t]+[A-Za-z0-9_./,:=@+\-]+)*", re.ASCII)


def command_tokens(command):
    if not isinstance(command, str) or not _COMMAND.fullmatch(command):
        return None
    tokens = command.split()
    if "=" in tokens[0] or any(token.startswith("=") for token in tokens):
        return None
    return tokens


def reported_commands(tool_call):
    if tool_call.get("kind") != "execute":
        return None
    raw = tool_call.get("rawInput")
    if not isinstance(raw, dict):
        return None
    command = raw.get("command")
    if not isinstance(command, str):
        return None
    parts = re.split(r"&&|\|\|", command)
    commands = [command_tokens(part.strip(" \t")) for part in parts]
    if not all(commands):
        return None
    names = [tokens[0] for tokens in commands]
    if "commands" in raw and raw["commands"] not in ([names[0]], names):
        return None
    return commands


def matching_command_prefixes(tool_call, prefixes):
    commands = reported_commands(tool_call)
    if not commands or not isinstance(prefixes, list):
        return None
    matches = []
    for tokens in commands:
        for prefix in prefixes:
            prefix_tokens = command_tokens(prefix)
            if prefix_tokens and tokens[:len(prefix_tokens)] == prefix_tokens:
                matches.append(" ".join(prefix_tokens))
                break
        else:
            return None
    return matches
