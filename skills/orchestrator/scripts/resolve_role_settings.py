#!/usr/bin/env python3
"""役割ごとの model と reasoning effort を安全に解決する。"""

import argparse
import json
import os
import re
import stat
import sys
from pathlib import Path


MAX_BYTES = 64 * 1024
ROLES = ("planner", "coder", "reviewer")
FIELDS = ("model", "reasoning_effort")
EFFORTS = {"low", "medium", "high", "xhigh", "max"}
MODEL_RE = re.compile(r"[A-Za-z0-9][-A-Za-z0-9._/]{0,127}\Z")
PROJECT_BASENAME = "orchestrator-defaults.yaml"
SHARED_DEFAULTS = Path(__file__).resolve().parents[1] / PROJECT_BASENAME
UNSET = object()


def fail(message):
    raise ValueError(message)


def regular_file_bytes(path, required):
    """リンクを辿らず、検査した通常ファイルと同じ inode だけを読む。"""
    try:
        before = os.lstat(path)
    except FileNotFoundError:
        if required:
            fail(f"設定ファイルがありません: {path}")
        return None
    if not stat.S_ISREG(before.st_mode):
        fail(f"設定ファイルは通常ファイルである必要があります: {path}")
    if not hasattr(os, "O_NOFOLLOW"):
        fail("この環境は O_NOFOLLOW を提供しないため設定を安全に読めません")
    flags = os.O_RDONLY | os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        fail(f"設定ファイルを安全に開けません: {path}: {error.strerror}")
    try:
        after = os.fstat(descriptor)
        if (not stat.S_ISREG(after.st_mode) or before.st_dev != after.st_dev
                or before.st_ino != after.st_ino):
            fail(f"設定ファイルが読取前後で変化しました: {path}")
        chunks = []
        remaining = MAX_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
    finally:
        os.close(descriptor)
    if len(data) > MAX_BYTES:
        fail(f"設定ファイルは 64 KiB 以下にしてください: {path}")
    return data


def parse_yaml(data, path, shared):
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        fail(f"設定ファイルは UTF-8 である必要があります: {path}: {error}")
    if text.startswith("\ufeff") or "\r" in text or "\t" in text:
        fail(f"設定ファイルに BOM、CR、tab は使えません: {path}")
    if any(ord(char) < 32 and char != "\n" for char in text):
        fail(f"設定ファイルに制御文字は使えません: {path}")
    try:
        text.encode("ascii")
    except UnicodeEncodeError:
        fail(f"設定ファイルは ASCII だけを使えます: {path}")
    lines = text.split("\n")
    if text.endswith("\n"):
        lines.pop()
    if not lines or any(not line or line.endswith(" ") for line in lines):
        fail(f"設定ファイルに空行または末尾空白は使えません: {path}")

    result = {}
    stage = 0
    last_role = -1
    current_role = None
    last_field = -1
    for number, line in enumerate(lines, 1):
        if line == "version: 1":
            if stage != 0:
                fail(f"version の位置または重複が不正です: {path}:{number}")
            stage = 1
            continue
        if line == "roles:":
            if stage != 1:
                fail(f"roles の位置または重複が不正です: {path}:{number}")
            stage = 2
            continue
        role_match = re.fullmatch(r"  (planner|coder|reviewer):", line)
        if role_match:
            if stage != 2:
                fail(f"role の位置が不正です: {path}:{number}")
            role = role_match.group(1)
            index = ROLES.index(role)
            if index <= last_role:
                fail(f"role の順序または重複が不正です: {path}:{number}")
            result[role] = {}
            current_role = role
            last_role = index
            last_field = -1
            continue
        field_match = re.fullmatch(r"    (model|reasoning_effort): ([A-Za-z0-9][-A-Za-z0-9._/]{0,127}|null)", line)
        if field_match and stage == 2 and current_role is not None:
            field, value = field_match.groups()
            index = FIELDS.index(field)
            if index <= last_field:
                fail(f"field の順序または重複が不正です: {path}:{number}")
            if value == "default":
                fail(f"YAML の default は使えません: {path}:{number}")
            if field == "reasoning_effort" and value not in EFFORTS | {"null"}:
                fail(f"reasoning_effort が不正です: {path}:{number}")
            result[current_role][field] = None if value == "null" else value
            last_field = index
            continue
        fail(f"許可されない YAML v1 の行です: {path}:{number}")

    if stage != 2:
        fail(f"version: 1 と roles: が必要です: {path}")
    if shared:
        if tuple(result) != ROLES or any(tuple(result[role]) != FIELDS for role in ROLES):
            fail(f"共通設定には全 role と model / reasoning_effort が必要です: {path}")
    elif not result or not any(fields for fields in result.values()):
        fail(f"プロジェクト設定には少なくとも一つの role と field が必要です: {path}")
    return result


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


def explicit_model(value):
    if value is UNSET:
        return UNSET
    if value == "default":
        return None
    if not MODEL_RE.fullmatch(value) or value == "default":
        fail("model が不正です")
    return value


def explicit_effort(value):
    if value is UNSET:
        return UNSET
    if value == "default":
        return None
    if value not in EFFORTS:
        fail("reasoning-effort が不正です")
    return value


def resolve_settings(workspace, role, model=UNSET, reasoning_effort=UNSET):
    if role not in ROLES:
        fail("role が不正です")
    workspace = checked_workspace(workspace)
    shared_path = Path(SHARED_DEFAULTS)
    shared = parse_yaml(regular_file_bytes(shared_path, required=True), shared_path, shared=True)
    project_path = workspace / PROJECT_BASENAME
    project_data = regular_file_bytes(project_path, required=False)
    project = parse_yaml(project_data, project_path, shared=False) if project_data is not None else {}
    explicit = {
        "model": explicit_model(model),
        "reasoning_effort": explicit_effort(reasoning_effort),
    }
    settings = {}
    sources = {}
    for field in FIELDS:
        if explicit[field] is not UNSET:
            settings[field] = explicit[field]
            sources[field] = "explicit"
        elif field in project.get(role, {}):
            settings[field] = project[role][field]
            sources[field] = "project"
        else:
            settings[field] = shared[role][field]
            sources[field] = "shared"
    return {
        "role": role,
        "settings": settings,
        "sources": sources,
        "files": {"shared": str(shared_path), "project": str(project_path) if project_data is not None else None},
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--role", choices=ROLES, required=True)
    parser.add_argument("--model", default=UNSET)
    parser.add_argument("--reasoning-effort", default=UNSET)
    args = parser.parse_args(argv)
    try:
        result = resolve_settings(args.workspace, args.role, args.model, args.reasoning_effort)
    except (OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
