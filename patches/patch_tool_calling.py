"""Reversible patch: make text-emulated tool calling more robust for agents.

- Lenient parsing of tool calls (```tool_call / ```function_call / ```json fences,
  <tool_call> tags, bare JSON), restricted to declared tool names.
- A short reminder appended to the end of long agent prompts.
- Tool results carry the tool name (looked up from tool_call_id).
- One retry when the model tells the user to run something instead of calling a tool.
"""
import sys
from pathlib import Path


MARKER = "gemini-web2api-sweden-installer: tool calling robustness"
BEGIN = f"# >>> {MARKER} >>>"
END = f"# <<< {MARKER} <<<"


def _block(body: str, indent: str = "") -> str:
    return f"{indent}{BEGIN}\n{body}{indent}{END}\n"


# ── tools.py ────────────────────────────────────────────────────────────────

TOOLS_OLD_HEAD = '''    parts = []
    images = []

    if tools and tool_choice != "none":
        tool_defs = []
'''
TOOLS_NEW_HEAD = '''    parts = []
    images = []
''' + _block('''    call_names = {}
    for msg in messages:
        for tc in msg.get("tool_calls") or []:
            call_names[tc.get("id")] = tc.get("function", {}).get("name", "")
    tool_names = []
''', "    ") + '''
    if tools and tool_choice != "none":
        tool_defs = []
'''

TOOLS_OLD_DEFS = '''        if tool_defs:
            constraint = _build_tool_choice_instruction(tool_choice, tool_defs)
'''
TOOLS_NEW_DEFS = _block('''        tool_names = [t["name"] for t in tool_defs if t["name"]]
''', "        ") + TOOLS_OLD_DEFS

TOOLS_OLD_RESULT = '''            parts.append(f"[Tool result for {msg.get('name', '')}]: {content}")
'''
TOOLS_NEW_RESULT = _block('''            name = msg.get("name") or call_names.get(msg.get("tool_call_id"), "")
            parts.append(f"[Tool result for {name}]: {content}")
''', "            ")

TOOLS_OLD_TAIL = '''    prompt = "\\n\\n".join(p for p in parts if p)
    return prompt, images
'''
TOOLS_NEW_TAIL = _block('''    if tool_names:
        parts.append(TOOL_REMINDER)
''', "    ") + TOOLS_OLD_TAIL

TOOLS_OLD_PARSE = '''def parse_tool_calls(text: str) -> tuple:
    """Extract tool_call blocks. Returns (clean_text, tool_calls_list)."""
    tool_calls = []
    pattern = r'```tool_call\\s*\\n(.*?)\\n```'
    clean_parts = []
    last_end = 0
    for m in re.finditer(pattern, text, re.DOTALL):
        clean_parts.append(text[last_end:m.start()])
        last_end = m.end()
        try:
            data = json.loads(m.group(1).strip())
            tool_calls.append({
                "id": f"call_{uuid.uuid4().hex[:8]}",
                "type": "function",
                "function": {
                    "name": data["name"],
                    "arguments": json.dumps(data.get("arguments", {}), ensure_ascii=False),
                },
            })
        except (json.JSONDecodeError, KeyError):
            pass
    clean_parts.append(text[last_end:])
    clean = "".join(clean_parts).strip()
    return clean, tool_calls
'''
TOOLS_NEW_PARSE = _block('''TOOL_REMINDER = (
    "[Reminder]: You are connected to the user's real machine through the tools listed above. "
    "If the request needs any action (running a command, reading/editing files, connecting to a server, etc.), "
    "do NOT tell the user to do it and do NOT claim you already did it — call the tool yourself by outputting "
    'ONLY ```tool_call\\n{"name": "<tool_name>", "arguments": {...}}\\n``` block(s). '
    "Answer with plain text only when no action is needed or after you have the tool results."
)

TOOL_RETRY_NUDGE = (
    "\\n\\n[System notice]: Your previous reply described an action or told the user to run something "
    "instead of doing it. You have tools that execute on the user's machine. Call the appropriate tool now "
    'using ONLY ```tool_call\\n{"name": "<tool_name>", "arguments": {...}}\\n``` block(s). '
    "If truly no tool is needed, reply with your final answer as text."
)

_DEFER_PATTERNS = [
    r"```(?:bash|sh|shell|zsh|powershell|pwsh|ps1|cmd|bat|console)\\b",
    r"你可以(?:直接)?(?:在|用|运行|执行|输入|敲)",
    r"(?:请|您可以|你需要)(?:在终端|在命令行)?(?:运行|执行|输入)",
    r"\\b(?:you can|you could|please) (?:run|execute|type|use the command)\\b",
    r"\\brun the following\\b",
]


def looks_like_deferred_action(text: str) -> bool:
    """Heuristic: model answered in prose/commands instead of calling a tool."""
    if not text:
        return False
    return any(re.search(p, text, re.IGNORECASE) for p in _DEFER_PATTERNS)


def last_message_is_user(messages: list) -> bool:
    for msg in reversed(messages or []):
        role = msg.get("role")
        if role in ("system", "developer"):
            continue
        return role == "user"
    return False


def tool_names_of(tools) -> list:
    names = []
    for t in tools or []:
        fn = t.get("function", t) if isinstance(t, dict) else {}
        if fn.get("name"):
            names.append(fn["name"])
    return names


def _tool_call_from_obj(data, tool_names):
    if not isinstance(data, dict):
        return None
    if isinstance(data.get("function"), dict):
        data = data["function"]
    name = data.get("name") or data.get("tool") or data.get("tool_name")
    if not isinstance(name, str) or not name:
        return None
    if tool_names and name not in tool_names:
        return None
    args = data.get("arguments", data.get("args", data.get("parameters", data.get("input", {}))))
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except json.JSONDecodeError:
            args = {"input": args}
    if args is None:
        args = {}
    return {
        "id": f"call_{uuid.uuid4().hex[:8]}",
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)},
    }


def _load_json_objects(raw: str):
    raw = raw.strip()
    try:
        data = json.loads(raw)
        return data if isinstance(data, list) else [data]
    except json.JSONDecodeError:
        pass
    objs, dec, i = [], json.JSONDecoder(), 0
    while i < len(raw):
        j = raw.find("{", i)
        if j < 0:
            break
        try:
            obj, end = dec.raw_decode(raw, j)
            objs.append(obj)
            i = end
        except json.JSONDecodeError:
            i = j + 1
    return objs


def parse_tool_calls(text: str, tool_names=None) -> tuple:
    """Extract tool calls. Returns (clean_text, tool_calls_list).

    Accepts ```tool_call / ```function_call / ```json fences, <tool_call> tags
    and a bare JSON reply. ```json fences and bare JSON only count when the
    name matches a declared tool.
    """
    tool_names = set(tool_names or [])
    tool_calls = []
    pattern = (r"```[ \\t]*(tool_call|tool_calls|function_call|json|tool)?[ \\t]*\\n?(.*?)```"
               r"|<tool_call>(.*?)</tool_call>")
    clean_parts = []
    last_end = 0
    for m in re.finditer(pattern, text, re.DOTALL):
        lang = m.group(1)
        body = m.group(2) if m.group(3) is None else m.group(3)
        if m.group(3) is None and lang is None:
            continue  # unlabeled fence: leave as text
        strict = lang in ("tool_call", "tool_calls", "function_call", "tool") or m.group(3) is not None
        found = []
        for obj in _load_json_objects(body or ""):
            tc = _tool_call_from_obj(obj, tool_names)
            if tc and (strict or tool_names):
                found.append(tc)
        if found or strict:
            clean_parts.append(text[last_end:m.start()])
            last_end = m.end()
            tool_calls.extend(found)
    clean_parts.append(text[last_end:])
    clean = "".join(clean_parts).strip()

    if not tool_calls and clean.startswith("{") and tool_names:
        found = [tc for tc in (_tool_call_from_obj(o, tool_names) for o in _load_json_objects(clean)) if tc]
        if found:
            tool_calls, clean = found, ""
    return clean, tool_calls


def parse_with_retry(text, tools, messages, prompt, gen_args, parser=None):
    """Parse tool calls; if the model deferred the action to the user, retry once."""
    from .gemini import generate, log

    parser = parser or parse_tool_calls
    names = tool_names_of(tools)
    clean, calls = parser(text, names)
    if calls or not names or not last_message_is_user(messages) or not looks_like_deferred_action(clean):
        return clean, calls
    log("Tool retry: model answered in prose instead of calling a tool")
    try:
        text2 = generate(prompt + "\\n\\n[Your previous reply]: " + clean + TOOL_RETRY_NUDGE, *gen_args)
    except Exception as e:
        log(f"Tool retry failed: {e}")
        return clean, calls
    clean2, calls2 = parser(text2 or "", names)
    return (clean2, calls2) if calls2 else (clean, calls)
''')

# ── server.py ───────────────────────────────────────────────────────────────

SERVER_OLD_CHAT = '''        if tools and text and tool_choice != "none":
            text, tool_calls = parse_tool_calls(text)
        msg = {"role": "assistant", "content": text or None}
'''
SERVER_NEW_CHAT = '''        if tools and text and tool_choice != "none":
''' + _block('''            from .tools import parse_with_retry
            text, tool_calls = parse_with_retry(text, tools, req.get("messages", []), prompt,
                                                (model_id, think_mode, file_refs, extra_fields), parse_tool_calls)
''', "            ") + '''        msg = {"role": "assistant", "content": text or None}
'''

SERVER_OLD_RESP = '''        if tools and text and tool_choice != "none":
            text, tool_calls = parse_tool_calls(text)

        rid ='''
SERVER_NEW_RESP = '''        if tools and text and tool_choice != "none":
''' + _block('''            from .tools import parse_with_retry
            text, tool_calls = parse_with_retry(text, tools, messages, prompt,
                                                (model_id, think_mode, file_refs, extra_fields), parse_tool_calls)
''', "            ") + '''
        rid ='''

PATCHES = {
    "tools.py": [
        (TOOLS_OLD_HEAD, TOOLS_NEW_HEAD),
        (TOOLS_OLD_DEFS, TOOLS_NEW_DEFS),
        (TOOLS_OLD_RESULT, TOOLS_NEW_RESULT),
        (TOOLS_OLD_TAIL, TOOLS_NEW_TAIL),
        (TOOLS_OLD_PARSE, TOOLS_NEW_PARSE),
    ],
    "server.py": [
        (SERVER_OLD_CHAT, SERVER_NEW_CHAT),
        (SERVER_OLD_RESP, SERVER_NEW_RESP),
    ],
}


def _patch_file(path: Path, pairs, action: str) -> str:
    if not path.is_file():
        raise SystemExit(f"上游文件不存在：{path}")
    text = path.read_text(encoding="utf-8")
    if MARKER in text:
        if any(new not in text for _, new in pairs):
            raise SystemExit(f"工具调用增强补丁标记存在但内容不匹配：{path}；为保护文件，停止自动更新。")
        if action == "remove":
            for old, new in pairs:
                text = text.replace(new, old, 1)
        return text
    if action == "remove":
        return text
    for old, new in pairs:
        if text.count(old) != 1:
            raise SystemExit(f"上游工具调用实现与预期不符：{path}；未修改。")
    for old, new in pairs:
        text = text.replace(old, new, 1)
    return text


def patch_tool_calling(package_dir: Path, action: str) -> None:
    if action not in ("apply", "remove"):
        raise SystemExit("action 必须是 apply 或 remove")
    # Validate both files before writing either one.
    results = {name: _patch_file(package_dir / name, pairs, action) for name, pairs in PATCHES.items()}
    for name, text in results.items():
        path = package_dir / name
        if path.read_text(encoding="utf-8") != text:
            path.write_text(text, encoding="utf-8")


def main() -> None:
    if len(sys.argv) != 3:
        raise SystemExit("usage: patch_tool_calling.py {apply|remove} /path/to/gemini_web2api")
    patch_tool_calling(Path(sys.argv[2]), sys.argv[1])


if __name__ == "__main__":
    main()
