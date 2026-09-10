#!/usr/bin/env python3
"""Enforce the two-attempt circuit breaker for a Claude Code session."""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path


STATE_DIRECTORY = Path.home() / ".claude" / "circuit-breaker"
RESET_PATTERN = re.compile(
    r"(?:サーキットブレーカー\s*(?:を)?\s*(?:解除|リセット)|circuit\s*breaker\s*(?:reset|clear))",
    re.IGNORECASE,
)


def load_input() -> dict:
    try:
        return json.load(sys.stdin)
    except json.JSONDecodeError:
        return {}


def state_path(session_id: str) -> Path:
    STATE_DIRECTORY.mkdir(parents=True, exist_ok=True)
    safe_id = re.sub(r"[^A-Za-z0-9_.-]", "_", session_id)
    return STATE_DIRECTORY / f"{safe_id}.json"


def load_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {"failures": {}, "tripped": None}


def signature(error: str) -> tuple[str, str]:
    lines = [line.strip() for line in error.splitlines() if line.strip()]
    if lines and re.fullmatch(r"Exit code \d+", lines[0]):
        lines.pop(0)
    stable = "\n".join(lines[:12]) or "Unknown tool failure"
    stable = re.sub(r"\x1b\[[0-9;]*m", "", stable)
    stable = re.sub(r"/(?:[^\s:]+/)+[^\s:]+", "<path>", stable)
    stable = re.sub(r"\b\d+\b", "#", stable)
    return hashlib.sha256(stable.encode()).hexdigest(), stable[:600]


def write_state(path: Path, state: dict) -> None:
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n")


def record(payload: dict) -> int:
    if payload.get("tool_name") not in {"Bash", "Edit", "Write"} or payload.get("is_interrupt"):
        return 0

    error = payload.get("error")
    session_id = payload.get("session_id")
    if not isinstance(error, str) or not session_id:
        return 0

    key, preview = signature(error)
    path = state_path(session_id)
    state = load_state(path)
    failure = state["failures"].setdefault(key, {"count": 0, "error": preview, "commands": []})
    failure["count"] += 1
    command = payload.get("tool_input", {}).get("command", "")
    if command:
        failure["commands"].append(command[:300])
        failure["commands"] = failure["commands"][-2:]

    if failure["count"] >= 2:
        state["tripped"] = {
            "error": failure["error"],
            "attempts": failure["commands"],
        }
    write_state(path, state)

    if state["tripped"]:
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "PostToolUseFailure",
                "additionalContext": (
                    "サーキットブレーカーが発動しました。同一エラーが2回発生したため、"
                    "以降の Bash・Edit・Write は拒否されます。Read で原因を確認し、"
                    "エラー、試行した2案、根本原因の仮説と選択肢を報告してください。"
                    "再開するには、ユーザーに「サーキットブレーカー解除」と指示してもらってください。"
                ),
            }
        }, ensure_ascii=False))
    return 0


def check(payload: dict) -> int:
    session_id = payload.get("session_id")
    if not session_id:
        return 0
    state = load_state(state_path(session_id))
    tripped = state.get("tripped")
    if not tripped:
        return 0
    print(
        "サーキットブレーカーが発動中です。3回目の修正・実行は許可されません。"
        "Read で調査し、ユーザーへ停止報告してください。明示的に再開する場合は、"
        "ユーザーに「サーキットブレーカー解除」と指示してもらってください。\n"
        f"対象エラー: {tripped.get('error', 'Unknown tool failure')}",
        file=sys.stderr,
    )
    return 2


def reset(payload: dict) -> int:
    prompt = payload.get("prompt", "")
    session_id = payload.get("session_id")
    if session_id and isinstance(prompt, str) and RESET_PATTERN.search(prompt):
        state_path(session_id).unlink(missing_ok=True)
        print("サーキットブレーカーをユーザー指示により解除しました。")
    return 0


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] not in {"record", "check", "reset"}:
        print("Usage: circuit-breaker.py {record|check|reset}", file=sys.stderr)
        return 1
    payload = load_input()
    return {"record": record, "check": check, "reset": reset}[sys.argv[1]](payload)


if __name__ == "__main__":
    raise SystemExit(main())
