#!/usr/bin/env python3
"""外部通信せずCLIの出力・失敗・待機を再現するテスト用ハーネス。"""

import json
import signal
import sys
import time
from pathlib import Path

args = sys.argv[1:]
prompt = sys.stdin.read()
if "--input-format" in args:
    prompt = json.loads(prompt)["message"]["content"]
is_agy = "--print-timeout" in args
if "--print" in args:
    if is_agy:
        instruction = args[args.index("--print") + 1]
        prompt = Path(instruction.removeprefix("Read ").removesuffix(
            " and perform only the delegated task described there.")).read_text()
is_stream = is_agy and "--output-format" in args
if is_stream:
    print(json.dumps({"event": "init", "conversation_id": "fake-agy"}), flush=True)
    if "COOPERATIVE" in prompt:
        def cancel(signum, frame):
            print(json.dumps({"event": "result", "result": {
                "conversation_id": "other-session" if "WRONG_ID" in prompt else "fake-agy",
                "status": "INTERRUPTED", "response": ""}}), flush=True)
            raise SystemExit(130)
        signal.signal(signal.SIGINT, cancel)
        while True:
            time.sleep(0.1)
if "HANG" in prompt:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    while True:
        time.sleep(0.1)
if "FAIL" in prompt:
    print("simulated failure", file=sys.stderr)
    raise SystemExit(7)
if "--output-last-message" in args:
    if "EMPTY" not in prompt:
        Path(args[args.index("--output-last-message") + 1]).write_text("FAKE_OK")
    print(json.dumps({"type": "thread.started", "thread_id": "fake-session"}))
elif is_stream:
    if "SUB_AGENT" in prompt:
        print(json.dumps({"event": "tool", "tool": {"name": "invoke_subagent"}}))
    print(json.dumps({"event": "result", "result": {"conversation_id": "fake-agy",
                     "status": "ERROR" if "JSON_ERROR" in prompt else "SUCCESS", "response": "FAKE_OK",
                     "denied_actions": [{"action": "read_file"}] if "DENIED" in prompt else []}}))
elif "--output-format" in args:
    print(json.dumps({"type": "result", "is_error": "JSON_ERROR" in prompt,
                      "result": "FAKE_OK", "session_id": "fake-session"}))
else:
    print("FAKE_OK")
