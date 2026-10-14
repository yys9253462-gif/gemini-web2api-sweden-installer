"""Contract tests for the installer's tool-calling robustness patch."""
import importlib.util
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PATCHER_PATH = ROOT / "patches" / "patch_tool_calling.py"
spec = importlib.util.spec_from_file_location("patch_tool_calling", PATCHER_PATH)
patcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(patcher)

FENCE = "`" * 3


# Minimal upstream-shaped fixtures: every OLD block exactly once.
TOOLS_FIXTURE = '''"""fixture"""
import json
import re
import uuid


def messages_to_prompt(messages: list, tools: list = None, tool_choice=None) -> tuple:
''' + patcher.TOOLS_OLD_HEAD + '''        for tool in tools:
            fn = tool.get("function", tool) if tool.get("type") == "function" else tool
            tool_defs.append({"name": fn.get("name", ""), "description": "", "parameters": {}})
''' + patcher.TOOLS_OLD_DEFS + '''            parts.append("# Tool Use " + json.dumps(tool_defs) + constraint)

    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if role == "tool":
''' + patcher.TOOLS_OLD_RESULT + '''        else:
            parts.append(content or "")

''' + patcher.TOOLS_OLD_TAIL + '''

def _build_tool_choice_instruction(tool_choice, tool_defs):
    return ""


''' + patcher.TOOLS_OLD_PARSE

SERVER_FIXTURE = '''def chat(self):
        tool_calls = None
''' + patcher.SERVER_OLD_CHAT + '''        return msg

def responses(self):
        tool_calls = None
''' + patcher.SERVER_OLD_RESP + ''' = 1
'''


class ToolCallingPatchTests(unittest.TestCase):
    def fixture(self):
        temp = tempfile.TemporaryDirectory(prefix="tool-calling-patch-")
        pkg = Path(temp.name) / "fixturepkg"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("", encoding="utf-8")
        (pkg / "tools.py").write_text(TOOLS_FIXTURE, encoding="utf-8")
        (pkg / "server.py").write_text(SERVER_FIXTURE, encoding="utf-8")
        self.addCleanup(temp.cleanup)
        return pkg

    def load_tools(self, pkg, generate=None):
        gemini = types.ModuleType("fixturepkg.gemini")
        gemini.generate = generate or (lambda *a: "")
        gemini.log = lambda *a: None
        sys.modules["fixturepkg"] = types.ModuleType("fixturepkg")
        sys.modules["fixturepkg"].__path__ = [str(pkg)]
        sys.modules["fixturepkg.gemini"] = gemini
        self.addCleanup(lambda: [sys.modules.pop(k, None) for k in
                                 ("fixturepkg", "fixturepkg.gemini", "fixturepkg.tools")])
        spec = importlib.util.spec_from_file_location("fixturepkg.tools", pkg / "tools.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules["fixturepkg.tools"] = mod
        spec.loader.exec_module(mod)
        return mod

    def test_apply_is_idempotent_and_reversible(self):
        pkg = self.fixture()
        patcher.patch_tool_calling(pkg, "apply")
        once = {n: (pkg / n).read_text(encoding="utf-8") for n in ("tools.py", "server.py")}
        patcher.patch_tool_calling(pkg, "apply")
        for name, text in once.items():
            self.assertEqual((pkg / name).read_text(encoding="utf-8"), text)
        patcher.patch_tool_calling(pkg, "remove")
        self.assertEqual((pkg / "tools.py").read_text(encoding="utf-8"), TOOLS_FIXTURE)
        self.assertEqual((pkg / "server.py").read_text(encoding="utf-8"), SERVER_FIXTURE)

    def test_refuses_unexpected_upstream_without_writing(self):
        pkg = self.fixture()
        (pkg / "server.py").write_text("unexpected", encoding="utf-8")
        with self.assertRaises(SystemExit):
            patcher.patch_tool_calling(pkg, "apply")
        self.assertEqual((pkg / "tools.py").read_text(encoding="utf-8"), TOOLS_FIXTURE)

    def test_lenient_parsing_respects_declared_tools(self):
        pkg = self.fixture()
        patcher.patch_tool_calling(pkg, "apply")
        tools = self.load_tools(pkg)
        names = ["pwsh"]
        for text in (
            FENCE + 'tool_call\n{"name": "pwsh", "arguments": {"command": "ls"}}\n' + FENCE,
            FENCE + 'json\n{"name": "pwsh", "args": {"command": "ls"}}\n' + FENCE,
            '<tool_call>{"name": "pwsh", "arguments": {"command": "ls"}}</tool_call>',
            '{"name": "pwsh", "arguments": "{\\"command\\": \\"ls\\"}"}',
        ):
            clean, calls = tools.parse_tool_calls(text, names)
            self.assertEqual(clean, "", text)
            self.assertEqual(calls[0]["function"]["name"], "pwsh")
            self.assertEqual(json.loads(calls[0]["function"]["arguments"]), {"command": "ls"})

        text = FENCE + 'json\n{"name": "other", "x": 1}\n' + FENCE
        self.assertEqual(tools.parse_tool_calls(text, names), (text, []))

    def test_prompt_names_tool_results_and_ends_with_reminder(self):
        pkg = self.fixture()
        patcher.patch_tool_calling(pkg, "apply")
        tools = self.load_tools(pkg)
        prompt, _ = tools.messages_to_prompt([
            {"role": "user", "content": "go"},
            {"role": "assistant", "tool_calls": [{"id": "c1", "function": {"name": "pwsh"}}]},
            {"role": "tool", "tool_call_id": "c1", "content": "done"},
        ], [{"type": "function", "function": {"name": "pwsh"}}])
        self.assertIn("[Tool result for pwsh]: done", prompt)
        self.assertTrue(prompt.endswith(tools.TOOL_REMINDER))

    def test_retries_once_when_model_defers_action_to_user(self):
        pkg = self.fixture()
        patcher.patch_tool_calling(pkg, "apply")
        prompts = []

        def fake_generate(prompt, *args):
            prompts.append(prompt)
            return FENCE + 'tool_call\n{"name": "pwsh", "arguments": {"command": "ssh tokyo"}}\n' + FENCE

        tools = self.load_tools(pkg, fake_generate)
        declared = [{"type": "function", "function": {"name": "pwsh"}}]
        user = [{"role": "user", "content": "连接服务器"}]
        deferred = "你可以直接在终端敲：\n" + FENCE + "bash\nssh tokyo\n" + FENCE

        clean, calls = tools.parse_with_retry(deferred, declared, user, "P", ("m",))
        self.assertEqual(len(prompts), 1)
        self.assertIn(tools.TOOL_RETRY_NUDGE, prompts[0])
        self.assertEqual(calls[0]["function"]["name"], "pwsh")

        tools.parse_with_retry("你好！", declared, user, "P", ("m",))
        after_tool = user + [{"role": "tool", "content": "ok"}]
        tools.parse_with_retry(deferred, declared, after_tool, "P", ("m",))
        self.assertEqual(len(prompts), 1)


if __name__ == "__main__":
    unittest.main()
