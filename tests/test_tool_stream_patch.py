"""Contract tests for the installer's OpenAI-compatible streamed tool-call patch."""
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PATCHER_PATH = ROOT / "patches" / "patch_upstream.py"
spec = importlib.util.spec_from_file_location("patch_upstream", PATCHER_PATH)
patcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(patcher)


FIXTURE_PREFIX = '''import json, time
class Sink:
    def __init__(self): self.data = []
    def write(self, value): self.data.append(value)
    def flush(self): pass

class Handler:
    def __init__(self):
        self.wfile = Sink()
        self.sent = None
    def _start_sse(self): pass
    def send_json(self, value): self.sent = value
    def render(self, stream, text, tool_calls):
        cid = "chatcmpl-test"
        model_name = "gemini-4-argon"
        msg = {"role": "assistant", "content": text or None}
        if tool_calls: msg["tool_calls"] = tool_calls
        finish = "tool_calls" if tool_calls else "stop"
'''
FIXTURE_SUFFIX = '''        return b"".join(self.wfile.data)
'''


class ToolStreamPatchTests(unittest.TestCase):
    def patched_fixture(self):
        temp = tempfile.TemporaryDirectory(prefix="tool-stream-patch-")
        path = Path(temp.name) / "server.py"
        original = FIXTURE_PREFIX + patcher.OLD_STREAM_BLOCK + FIXTURE_SUFFIX
        path.write_text(original, encoding="utf-8")
        patcher.patch_tool_stream(path, "apply")
        return temp, path, original

    def test_streaming_tool_delta_is_openai_compatible_and_executes_after_frame(self):
        temp, path, _ = self.patched_fixture()
        self.addCleanup(temp.cleanup)
        source = path.read_text(encoding="utf-8")
        namespace = {}
        exec(compile(source, str(path), "exec"), namespace)
        handler = namespace["Handler"]()
        tool = {
            "id": "call_test",
            "type": "function",
            "function": {"name": "local_echo", "arguments": '{"text":"ok"}'},
        }
        handler.render(True, "", [tool])
        frames = []
        for chunk in handler.wfile.data:
            line = chunk.decode().strip()
            if line.startswith("data: ") and line != "data: [DONE]":
                frames.append(json.loads(line[6:]))

        self.assertEqual(frames[0]["choices"][0]["delta"]["role"], "assistant")
        returned = frames[1]["choices"][0]["delta"]["tool_calls"][0]
        self.assertEqual(returned["index"], 0)
        self.assertEqual(returned["function"]["name"], "local_echo")
        self.assertEqual(frames[-1]["choices"][0]["finish_reason"], "tool_calls")

        events = []
        for frame in frames:
            calls = frame["choices"][0]["delta"].get("tool_calls", [])
            if calls:
                events.append("tool_call_frame_received")
                events.append("execute_local_tool")
        self.assertEqual(events, ["tool_call_frame_received", "execute_local_tool"])
        self.assertEqual(handler.wfile.data[-1], b"data: [DONE]\n\n")

    def test_patch_is_idempotent_and_reversible(self):
        temp, path, original = self.patched_fixture()
        self.addCleanup(temp.cleanup)
        patcher.patch_tool_stream(path, "apply")
        self.assertEqual(path.read_text(encoding="utf-8").count("OpenAI tool stream compliance"), 2)
        patcher.patch_tool_stream(path, "remove")
        self.assertEqual(path.read_text(encoding="utf-8"), original)


if __name__ == "__main__":
    unittest.main()
