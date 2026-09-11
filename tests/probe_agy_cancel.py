#!/usr/bin/env python3
"""実機検証専用。会話ID取得後に取消し、モデルの中断結果を確認する。"""

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task-id", required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    runner = root / "skills/orchestrator/scripts/external_runner.py"
    command = [sys.executable, str(runner), "run", "--workspace", str(root),
               "--task-id", args.task_id, "--harness", "agy", "--role", "reviewer",
               "--access", "workspace-write", "--prompt-file", str(root / "tests/fixtures/smoke-request.md"),
               "--timeout", "40", "--cancel-grace", "10"]
    with subprocess.Popen(command, stdout=subprocess.PIPE, text=True) as process:
        first = json.loads(process.stdout.readline())
        print(json.dumps(first, ensure_ascii=False), flush=True)
        if "job" not in first:
            return 2
        job = Path(first["job"])
        while process.poll() is None:
            state = json.loads((job / "state.json").read_text())
            if state.get("conversation_id") and state["status"] == "running":
                subprocess.run([sys.executable, str(runner), "cancel", "--job", str(job)], check=True)
                break
            time.sleep(0.05)
        # run自身の40秒制限と停止猶予があるため、ここでは出力を最後まで回収する。
        remaining = process.communicate()[0]
        print(remaining, end="", flush=True)
        final = json.loads((job / "state.json").read_text())
        result = json.loads((job / "harness-result.json").read_text()) if (job / "harness-result.json").exists() else {}
        interrupted = (final.get("harness_status") in {"INTERRUPTED", "CANCELED"}
                       or (final.get("harness_status") == "ERROR" and result.get("error") == "interrupted"))
        confirmed = (final["status"] == "cancelled" and final.get("stop_confirmed")
                     and interrupted)
        return 0 if confirmed else 1


if __name__ == "__main__":
    raise SystemExit(main())
