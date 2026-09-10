#!/usr/bin/env python3
"""外部通信せずCLIの出力・失敗・待機を再現するテスト用ハーネス。"""

import json
import signal
import sys
import time
from pathlib import Path

args = sys.argv[1:]
prompt = sys.stdin.read()
if "--print" in args:
    if "--output-format" not in args:
        instruction = args[args.index("--print") + 1]
        prompt = Path(instruction.removeprefix("Read ").removesuffix(
            " and perform only the delegated task described there.")).read_text()
if "HANG" in prompt:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    while True:
        time.sleep(0.1)
if "FAIL" in prompt:
    print("simulated failure", file=sys.stderr)
    raise SystemExit(7)
if "--output-last-message" in args:
    if "EMPTY" not in prompt:
        Path(args[args.index("--output-last-message") + 1]).write_text("FAKE_OK")
    print(json.dumps({"type": "thread.started", "thread_id": "fake-session"}))
elif "--output-format" in args:
    print(json.dumps({"type": "result", "is_error": "JSON_ERROR" in prompt,
                      "result": "FAKE_OK", "session_id": "fake-session"}))
else:
    print("FAKE_OK")
