"""Apply reversible, narrowly scoped compatibility patches to upstream files."""
import sys
from pathlib import Path


OLD_STREAM_BLOCK = '''        if stream:
            self._start_sse()
            chunk = {"id": cid, "object": "chat.completion.chunk", "created": int(time.time()),
                     "model": model_name, "choices": [{"index": 0, "delta": msg, "finish_reason": finish}]}
            self.wfile.write(f"data: {json.dumps(chunk, ensure_ascii=False)}\\n\\n".encode())
            self.wfile.write(b"data: [DONE]\\n\\n")
            self.wfile.flush()
'''

NEW_STREAM_BLOCK = '''        if stream:
            # >>> gemini-web2api-sweden-installer: OpenAI tool stream compliance >>>
            self._start_sse()

            def emit_delta(delta, finish_reason=None):
                chunk = {"id": cid, "object": "chat.completion.chunk", "created": int(time.time()),
                         "model": model_name, "choices": [{"index": 0, "delta": delta,
                                                               "finish_reason": finish_reason}]}
                self.wfile.write(f"data: {json.dumps(chunk, ensure_ascii=False)}\\n\\n".encode())

            emit_delta({"role": "assistant"})
            if tool_calls:
                if text:
                    emit_delta({"content": text})
                for index, tool_call in enumerate(tool_calls):
                    delta_call = {"index": index, **tool_call}
                    emit_delta({"tool_calls": [delta_call]})
                emit_delta({}, "tool_calls")
            else:
                if text:
                    emit_delta({"content": text})
                emit_delta({}, "stop")
            self.wfile.write(b"data: [DONE]\\n\\n")
            self.wfile.flush()
            # <<< gemini-web2api-sweden-installer: OpenAI tool stream compliance <<<
'''

MARKER = "gemini-web2api-sweden-installer: OpenAI tool stream compliance"


def patch_tool_stream(path: Path, action: str) -> None:
    if not path.is_file():
        raise SystemExit(f"上游 Chat Completions 文件不存在：{path}")
    text = path.read_text(encoding="utf-8")
    if action == "remove":
        if MARKER in text:
            if NEW_STREAM_BLOCK not in text:
                raise SystemExit("工具流式补丁标记存在但内容不匹配；为保护文件，停止自动更新。")
            path.write_text(text.replace(NEW_STREAM_BLOCK, OLD_STREAM_BLOCK, 1), encoding="utf-8")
        return
    if action != "apply":
        raise SystemExit("action 必须是 apply 或 remove")
    if MARKER in text:
        if NEW_STREAM_BLOCK not in text:
            raise SystemExit("工具流式补丁标记存在但内容不匹配。")
        return
    if text.count(OLD_STREAM_BLOCK) != 1:
        raise SystemExit("上游工具流式实现与预期不符；未修改。")
    path.write_text(text.replace(OLD_STREAM_BLOCK, NEW_STREAM_BLOCK, 1), encoding="utf-8")


def main() -> None:
    if len(sys.argv) != 3:
        raise SystemExit("usage: patch_upstream.py {apply|remove} /path/to/server.py")
    patch_tool_stream(Path(sys.argv[2]), sys.argv[1])


if __name__ == "__main__":
    main()
