#!/usr/bin/env python3
"""orchestrator の役割設定を一度だけ固定し、安全に保存する。"""

import argparse
import copy
import json
import os
import stat
import sys
from pathlib import Path

from resolve_role_settings import ROLES, UNSET, resolve_settings


ROLE_FIELDS = ("harness", "model", "reasoning_effort", "sources")
OVERRIDE_FIELDS = {"harness", "model", "reasoning_effort"}


def fail(message):
    raise ValueError(message)


def validate_explicit_roles(explicit_roles):
    if not isinstance(explicit_roles, dict):
        fail("overrides は object である必要があります")
    unknown_roles = set(explicit_roles) - set(ROLES)
    if unknown_roles:
        fail("未知の role があります")
    normalized = {}
    for role in ROLES:
        value = explicit_roles.get(role, {})
        if not isinstance(value, dict) or set(value) - OVERRIDE_FIELDS:
            fail("role override が不正です")
        normalized[role] = {
            "harness": value.get("harness", UNSET),
            "model": value.get("model", UNSET),
            "reasoning_effort": value.get("reasoning_effort", UNSET),
        }
    return normalized


def validate_files(files):
    if not isinstance(files, dict) or set(files) != {"shared", "project"}:
        fail("role_settings_files が不正です")
    if not isinstance(files["shared"], str) or (files["project"] is not None and not isinstance(files["project"], str)):
        fail("role_settings_files が不正です")


def validate_record(record, allow_legacy_source=False):
    if not isinstance(record, dict) or set(record) != set(ROLE_FIELDS):
        fail("roles record が不正です")
    if record["harness"] not in {"codex", "claude", "agy"}:
        fail("roles.harness が不正です")
    if record["model"] is not None and not isinstance(record["model"], str):
        fail("roles.model が不正です")
    if record["reasoning_effort"] is not None and record["reasoning_effort"] not in {"low", "medium", "high", "xhigh", "max"}:
        fail("roles.reasoning_effort が不正です")
    sources = record["sources"]
    permitted = {"harness", "model", "reasoning_effort"} if not allow_legacy_source else {"model", "reasoning_effort"}
    if not isinstance(sources, dict) or set(sources) != permitted:
        fail("roles.sources が不正です")
    if any(value not in {"explicit", "project", "shared", "legacy"} for value in sources.values()):
        fail("roles.sources が不正です")
    if record["harness"] == "agy" and record["reasoning_effort"] in {"xhigh", "max"}:
        fail("agy の reasoning-effort が不正です")


def normalize_roles(state):
    roles = state.get("roles")
    if not isinstance(roles, dict) or set(roles) != set(ROLES):
        fail("roles が不正です")
    if all(isinstance(roles[role], dict) for role in ROLES):
        normalized = copy.deepcopy(roles)
        migrated = False
        for record in normalized.values():
            if isinstance(record, dict) and set(record) == set(ROLE_FIELDS) and isinstance(record.get("sources"), dict) and set(record["sources"]) == {"model", "reasoning_effort"}:
                record["sources"]["harness"] = "legacy"
                migrated = True
            validate_record(record)
        return normalized, migrated
    if not all(isinstance(roles[role], str) for role in ROLES):
        fail("legacy roles が不正です")
    settings = state.get("role_settings")
    if not isinstance(settings, dict) or set(settings) != set(ROLES):
        fail("legacy role_settings が不正です")
    normalized = {}
    for role in ROLES:
        setting = settings[role]
        if not isinstance(setting, dict) or set(setting) != {"model", "reasoning_effort", "sources"}:
            fail("legacy role_settings が不正です")
        sources = setting["sources"]
        if not isinstance(sources, dict) or set(sources) != {"model", "reasoning_effort"}:
            fail("legacy role_settings が不正です")
        normalized[role] = {
            "harness": roles[role],
            "model": setting["model"],
            "reasoning_effort": setting["reasoning_effort"],
            "sources": {"harness": "legacy", **copy.deepcopy(sources)},
        }
        validate_record(normalized[role])
    return normalized, True


def resolve_initial_roles(workspace, explicit_roles, resolve=resolve_settings):
    overrides = validate_explicit_roles(explicit_roles)
    roles = {}
    files = None
    for role in ROLES:
        override = overrides[role]
        result = resolve(workspace, role, override["model"], override["reasoning_effort"],
                         harness=override["harness"])
        if not isinstance(result, dict) or result.get("role") != role:
            fail("resolver の結果が不正です")
        settings = result.get("settings")
        sources = result.get("sources")
        if not isinstance(settings, dict) or list(settings) != ["harness", "model", "reasoning_effort"]:
            fail("resolver の settings が不正です")
        if not isinstance(sources, dict) or list(sources) != ["harness", "model", "reasoning_effort"]:
            fail("resolver の sources が不正です")
        record = {**settings, "sources": sources}
        validate_record(record)
        current_files = result.get("files")
        validate_files(current_files)
        if files is None:
            files = copy.deepcopy(current_files)
        elif files != current_files:
            fail("role ごとの設定ファイルが一致しません")
        roles[role] = record
    return {"roles": roles, "role_settings_files": files}


def roles_for_continuation(state):
    if not isinstance(state, dict):
        fail("state が object ではありません")
    roles, migrated = normalize_roles(state)
    validate_files(state.get("role_settings_files"))
    return roles, migrated


def initialize_roles(state, workspace, explicit_roles, resolve=resolve_settings):
    if not isinstance(state, dict):
        fail("state が object ではありません")
    if "roles" in state:
        roles, migrated = roles_for_continuation(state)
        result = copy.deepcopy(state)
        result["roles"] = roles
        if migrated:
            result.pop("role_settings", None)
        return result
    if "role_settings" in state:
        fail("role_settings だけの state は初期化できません")
    resolved = resolve_initial_roles(workspace, explicit_roles, resolve)
    result = copy.deepcopy(state)
    result.update(resolved)
    return result


def resume_roles(state, workspace, explicit_roles, reason, resolve=resolve_settings):
    if not isinstance(reason, str) or not reason.strip():
        fail("resume reason が必要です")
    if not isinstance(state, dict) or state.get("active_child") is not None:
        fail("active_child がある間は resume できません")
    old_roles, migrated = roles_for_continuation(state)
    resolved = resolve_initial_roles(workspace, explicit_roles, resolve)
    result = copy.deepcopy(state)
    result["roles"] = resolved["roles"]
    result["role_settings_files"] = resolved["role_settings_files"]
    result.pop("role_settings", None)
    history = result.setdefault("role_settings_history", [])
    if not isinstance(history, list):
        fail("role_settings_history が不正です")
    history.append({
        "reason": reason,
        "previous_roles": old_roles,
        "previous_files": copy.deepcopy(state["role_settings_files"]),
        "resumed_roles": copy.deepcopy(resolved["roles"]),
        "resumed_files": copy.deepcopy(resolved["role_settings_files"]),
    })
    return result


def atomic_write_state(path, state):
    path = Path(path)
    parent = path.parent
    if parent.is_symlink() or not parent.is_dir():
        fail("state directory が不正です")
    try:
        existing = os.lstat(path)
    except FileNotFoundError:
        existing = None
    if existing is not None and not stat.S_ISREG(existing.st_mode):
        fail("state path は通常ファイルである必要があります")
    temporary = path.with_name(path.name + ".tmp")
    try:
        temporary_stat = os.lstat(temporary)
    except FileNotFoundError:
        temporary_stat = None
    if temporary_stat is not None:
        fail("state temporary が既に存在します")
    data = (json.dumps(state, ensure_ascii=False, indent=2) + "\n").encode()
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            fail("state temporary は通常ファイルである必要があります")
        written = 0
        while written < len(data):
            count = os.write(descriptor, data[written:])
            if count <= 0:
                raise OSError("state temporary への書込みが完了しません")
            written += count
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.replace(temporary, path)
    except Exception:
        if descriptor is not None:
            os.close(descriptor)
        try:
            temporary_info = os.lstat(temporary)
            if stat.S_ISREG(temporary_info.st_mode):
                os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def read_json_object(path, label):
    path = Path(path)
    info = os.lstat(path)
    if not stat.S_ISREG(info.st_mode) or path.is_symlink() or info.st_size > 64 * 1024:
        fail(f"{label} は 64 KiB 以下の通常ファイルである必要があります")
    with path.open(encoding="utf-8") as source:
        value = json.load(source)
    if not isinstance(value, dict):
        fail(f"{label} は JSON object である必要があります")
    return value


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("init", "resume"):
        command = commands.add_parser(name)
        command.add_argument("--workspace", type=Path, required=True)
        command.add_argument("--state", type=Path, required=True)
        command.add_argument("--overrides-file", type=Path, required=True)
        if name == "resume":
            command.add_argument("--reason", required=True)
    command = commands.add_parser("continue")
    command.add_argument("--state", type=Path, required=True)
    command.add_argument("--role", choices=ROLES, required=True)
    args = parser.parse_args(argv)
    try:
        state = read_json_object(args.state, "state")
        if args.command == "continue":
            roles, migrated = roles_for_continuation(state)
            if migrated:
                state["roles"] = roles
                state.pop("role_settings", None)
                atomic_write_state(args.state, state)
            print(json.dumps(roles[args.role], ensure_ascii=False, separators=(",", ":")))
            return 0
        overrides = read_json_object(args.overrides_file, "overrides")
        if args.command == "init":
            result = initialize_roles(state, args.workspace, overrides)
        else:
            result = resume_roles(state, args.workspace, overrides, args.reason)
        atomic_write_state(args.state, result)
        print(json.dumps(result["roles"], ensure_ascii=False, separators=(",", ":")))
        return 0
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(str(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
