#!/usr/bin/env python3
"""確認済みの機能から委任方式を選ぶ。エージェント自体は起動しない。"""

import argparse
import json


def select_route(parent, target, native_ready=False, external_ready=False,
                 require_external=False, mode="auto"):
    if mode not in {"auto", "native", "external"}:
        raise ValueError(f"未知の mode: {mode}")
    native_allowed = parent == target and native_ready and not require_external
    if mode in {"auto", "native"} and native_allowed:
        return {"status": "ready", "transport": "native", "harness": target,
                "reason": "同一ハーネスで必要な標準委任機能を確認済み"}
    if mode in {"auto", "external"} and external_ready:
        return {"status": "ready", "transport": "external", "harness": target,
                "reason": "対象ハーネスの外部委任機能を確認済み"}
    return {"status": "blocked", "transport": None, "harness": target,
            "reason": "指定条件で利用できる委任手段が未確認または未対応"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parent", required=True, choices=["codex", "claude", "agy"])
    parser.add_argument("--target", required=True, choices=["codex", "claude", "agy"])
    parser.add_argument("--native-ready", action="store_true")
    parser.add_argument("--external-ready", action="store_true")
    parser.add_argument("--require-external", action="store_true")
    parser.add_argument("--mode", choices=["auto", "native", "external"], default="auto")
    args = parser.parse_args()
    result = select_route(**vars(args))
    print(json.dumps(result, ensure_ascii=False))
    raise SystemExit(0 if result["status"] == "ready" else 2)
