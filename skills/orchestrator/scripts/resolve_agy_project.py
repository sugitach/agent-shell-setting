#!/usr/bin/env python3
"""workspace ごとの Agy Project ID を安全に解決する。"""

import argparse
import json
import os
import re
import stat
import sys
from pathlib import Path


MAX_BYTES = 4 * 1024
PROJECT_FILE = Path(".orchestration") / "agy-project.json"
PROJECT_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")


def fail(message):
    raise ValueError(message)


def checked_workspace(workspace):
    if not workspace.is_absolute():
        fail("workspace は絶対パスで指定してください")
    try:
        resolved = workspace.resolve(strict=True)
    except OSError as error:
        fail(f"workspace を解決できません: {error}")
    if not resolved.is_dir():
        fail("workspace は実ディレクトリである必要があります")
    return resolved


def regular_file_bytes(path):
    """リンクを辿らず、検査した通常ファイルと同じ inode だけを読む。"""
    try:
        before = os.lstat(path)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(before.st_mode):
        fail(f"agy project 設定は通常ファイルである必要があります: {path}")
    if not hasattr(os, "O_NOFOLLOW"):
        fail("この環境は O_NOFOLLOW を提供しないため agy project 設定を安全に読めません")
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as error:
        fail(f"agy project 設定を安全に開けません: {path}: {error.strerror}")
    try:
        after = os.fstat(descriptor)
        if (not stat.S_ISREG(after.st_mode) or before.st_dev != after.st_dev
                or before.st_ino != after.st_ino):
            fail(f"agy project 設定が読取前後で変化しました: {path}")
        data = os.read(descriptor, MAX_BYTES + 1)
    finally:
        os.close(descriptor)
    if len(data) > MAX_BYTES:
        fail(f"agy project 設定は 4 KiB 以下にしてください: {path}")
    return data


def valid_project_id(value, source):
    if not isinstance(value, str) or not PROJECT_ID_RE.fullmatch(value):
        fail(f"agy project id が不正です: {source}")
    return value


def parse_project_file(data, path):
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        fail(f"agy project 設定は UTF-8 である必要があります: {path}: {error}")
    if any(ord(char) < 32 and char not in "\n\t" for char in text):
        fail(f"agy project 設定に制御文字は使えません: {path}")
    try:
        value = json.loads(text, object_pairs_hook=lambda pairs: dict(pairs) if len({key for key, _ in pairs}) == len(pairs) else (_ for _ in ()).throw(ValueError("重複キー")))
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        fail(f"agy project 設定が JSON object ではありません: {path}: {error}")
    if not isinstance(value, dict) or set(value) != {"project_id"}:
        fail(f"agy project 設定は project_id だけを持つ JSON object である必要があります: {path}")
    return valid_project_id(value["project_id"], str(path))


def resolve_agy_project(workspace, explicit=None):
    """明示引数、環境変数、workspace ファイルの順で Project ID を解決する。"""
    workspace = checked_workspace(workspace)
    if explicit is not None:
        return valid_project_id(explicit, "--agy-project")
    environment = os.environ.get("AGY_PROJECT_ID")
    if environment is not None:
        return valid_project_id(environment, "AGY_PROJECT_ID")
    path = workspace / PROJECT_FILE
    data = regular_file_bytes(path)
    if data is not None:
        return parse_project_file(data, path)
    fail("agy project id could not be resolved")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--agy-project")
    args = parser.parse_args(argv)
    try:
        print(json.dumps({"project_id": resolve_agy_project(args.workspace, args.agy_project)}, separators=(",", ":")))
        return 0
    except (OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
