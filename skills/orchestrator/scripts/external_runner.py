#!/usr/bin/env python3
"""外部ハーネスを実行し、ジョブ単位の結果・状態・取消要求を扱う。"""

import argparse
import fcntl
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from pathlib import Path


SKILLS = Path(__file__).resolve().parents[2]
ACTIVE = {"starting", "running", "unknown"}


def write_json(path, value):
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(path)


@contextmanager
def task_lock(task):
    with (task / "external.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ValueError("同一タスクの外部子が稼働中です") from error
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def command_for(harness, executable, workspace, job, access, model, timeout):
    if harness == "codex":
        command = [executable, "exec", "--json", "--color", "never", "--cd", str(workspace),
                   "--sandbox", access, "-c", 'approval_policy="never"',
                   "--output-last-message", str(job / "response.md")]
        if model:
            command += ["--model", model]
        return command + ["-"]
    if harness == "claude":
        command = [executable, "--print", "--output-format", "json",
                   "--permission-mode", "dontAsk" if access == "read-only" else "acceptEdits",
                   "--disallowedTools", "Agent,Task"]
        if access == "read-only":
            command += ["--tools", "Read,Glob,Grep"]
        if model:
            command += ["--model", model]
        return command
    # agy の sandbox は read-only と同義ではないため明示的に区別する。
    if access == "read-only":
        raise ValueError("agy CLI に read-only 強制オプションを確認できません。workspace-write の明示が必要です")
    command = [executable, "--sandbox", "--print-timeout", f"{max(1, int(timeout))}s"]
    if model:
        command += ["--model", model]
    return command + ["--print", f"Read {job / 'request.md'} and perform only the delegated task described there."]


def stop_group(process):
    # PIDを外部の状態ファイルから取得せず、自分が起動したグループだけを停止する。
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        process.wait()
        return
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        process.poll()
        try:
            os.killpg(process.pid, 0)
        except ProcessLookupError:
            process.wait()
            return
        time.sleep(0.05)
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()


def collect_result(harness, job):
    if harness == "claude":
        result = json.loads((job / "stdout.log").read_text())
        if not isinstance(result, dict) or result.get("is_error"):
            raise ValueError("Claude がエラー結果を返しました")
        if result.get("permission_denials"):
            raise ValueError("Claude に未解決の権限拒否があります")
        response = result.get("result", "")
        if not isinstance(response, str):
            raise ValueError("Claude の result が文字列ではありません")
        (job / "response.md").write_text(response)
    elif harness == "agy":
        shutil.copyfile(job / "stdout.log", job / "response.md")
    if not (job / "response.md").is_file() or not (job / "response.md").read_text().strip():
        raise ValueError("最終回答がありません")


def run(args):
    workspace = args.workspace.resolve(strict=True)
    if not workspace.is_dir() or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", args.task_id):
        raise ValueError("workspace または task-id が不正です")
    if not 0 < args.timeout <= 86400:
        raise ValueError("timeout は0秒より大きく86400秒以下で指定してください")
    prompt = args.prompt_file.read_text()
    role_skill = SKILLS / args.role / "SKILL.md"
    role_text = role_skill.read_text()
    executable = shutil.which(args.harness)
    if not executable:
        raise ValueError(f"{args.harness} が PATH にありません")
    task = workspace / ".orchestration" / args.task_id
    # 管理用ディレクトリをリンクで作業範囲外へ誘導しない。
    for part in (workspace / ".orchestration", task, task / "jobs"):
        if part.is_symlink():
            raise ValueError(f"管理用ディレクトリがリンクです: {part}")
        part.mkdir(mode=0o700, exist_ok=True)
    with task_lock(task):
        parent_state = task / "state.json"
        if parent_state.exists():
            parent = json.loads(parent_state.read_text())
            if parent.get("phase") == "blocked" or parent.get("blocked_reason"):
                raise ValueError("親タスクが blocked です。解除・確認前に起動できません")
        for previous in (task / "jobs").glob("*/state.json"):
            if json.loads(previous.read_text()).get("status") in ACTIVE:
                raise ValueError(f"未解決の旧ジョブがあります。停止確認が必要です: {previous.parent}")
        job = task / "jobs" / uuid.uuid4().hex
        # 引数の検証はジョブ作成前に終える。
        command = command_for(args.harness, executable, workspace, job,
                              args.access, args.model, args.timeout)
        job.mkdir(mode=0o700)
        (job / "request.md").write_text(
            f"あなたは子セッションです。task_id={args.task_id}, role={args.role}。再委任は禁止。\n"
            "以下の担当スキルに従い、最後の回答に担当の結果を返してください。\n"
            "親の役割を引き受けず、commit/push/PR作成は行わないでください。\n\n"
            + role_text + "\n\n## 親からの依頼\n\n" + prompt)
        state = {"job": str(job), "task_id": args.task_id, "role": args.role,
                 "harness": args.harness, "transport": "external", "status": "starting",
                 "workspace": str(workspace), "access": args.access,
                 "runner_pid": os.getpid(), "child_pid": None,
                 "started_at": time.time(), "exit_code": None}
        write_json(job / "state.json", state)
        print(json.dumps(state, ensure_ascii=False), flush=True)
        interrupted = False

        def interrupt(signum, frame):
            nonlocal interrupted
            interrupted = True

        handlers = {sig: signal.signal(sig, interrupt) for sig in (signal.SIGTERM, signal.SIGINT)}
        process = None
        cleaned_up = False
        try:
            with (job / "request.md").open() as request, (job / "stdout.log").open("w") as out, (job / "stderr.log").open("w") as err:
                if interrupted or (job / "cancel.request").exists():
                    state["status"] = "cancelled"
                else:
                    process = subprocess.Popen(command, cwd=workspace,
                                               stdin=request if args.harness != "agy" else subprocess.DEVNULL,
                                               stdout=out, stderr=err, start_new_session=True)
                    state.update(status="running", child_pid=process.pid)
                    write_json(job / "state.json", state)
                    deadline = time.monotonic() + args.timeout
                    while process.poll() is None:
                        if interrupted or (job / "cancel.request").exists():
                            state["status"] = "cancelled"
                            break
                        if time.monotonic() >= deadline:
                            state["status"] = "timed_out"
                            break
                        time.sleep(0.05)
                    # 正常終了でも残った同一グループの子プロセスを回収する。
                    stop_group(process)
                    cleaned_up = True
                    state["exit_code"] = process.returncode
                    if interrupted or (job / "cancel.request").exists():
                        state["status"] = "cancelled"
            if state["status"] == "running":
                if process.returncode:
                    raise ValueError(f"CLI が終了コード {process.returncode} を返しました")
                collect_result(args.harness, job)
                state["status"] = "completed"
            elif args.harness == "agy" and process is not None:
                state.update(status="unknown", error="CLI は停止しましたが agy 本体の停止は未確認です")
        except Exception as error:
            state.update(status="unknown" if args.harness == "agy" and process else "failed", error=str(error))
        finally:
            if process is not None and not cleaned_up:
                try:
                    stop_group(process)
                except OSError as error:
                    state.update(status="unknown", error=f"子の停止未確認: {error}")
            if state["status"] == "completed" and (interrupted or (job / "cancel.request").exists()):
                state["status"] = "unknown" if args.harness == "agy" else "cancelled"
            state["finished_at"] = time.time()
            write_json(job / "state.json", state)
            for sig, handler in handlers.items():
                signal.signal(sig, handler)
        print(json.dumps(state, ensure_ascii=False), flush=True)
        return 0 if state["status"] == "completed" else 1


def inspect_job(job):
    job = job.resolve(strict=True)
    state = json.loads((job / "state.json").read_text())
    if state.get("job") != str(job):
        raise ValueError("ジョブの保存先とIDが一致しません")
    if state["status"] in {"starting", "running"}:
        try:
            with task_lock(job.parent.parent):
                state.update(status="unknown", error="ランナーのロックがありません。旧子の停止確認が必要です")
        except ValueError:
            pass
    return state


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    execute = commands.add_parser("run")
    execute.add_argument("--workspace", type=Path, required=True)
    execute.add_argument("--task-id", required=True)
    execute.add_argument("--harness", choices=["codex", "claude", "agy"], required=True)
    execute.add_argument("--role", choices=["planner", "coder", "reviewer"], required=True)
    execute.add_argument("--prompt-file", type=Path, required=True)
    execute.add_argument("--access", choices=["read-only", "workspace-write"], default="read-only")
    execute.add_argument("--model")
    execute.add_argument("--timeout", type=float, default=600)
    for name in ("status", "cancel"):
        sub = commands.add_parser(name)
        sub.add_argument("--job", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "run":
            return run(args)
        state = inspect_job(args.job)
        if args.command == "cancel" and state["status"] in {"starting", "running"}:
            (args.job / "cancel.request").touch(exist_ok=True)
            state["cancel_requested"] = True
        print(json.dumps(state, ensure_ascii=False))
        return 2 if state["status"] == "unknown" else 0
    except (ValueError, OSError) as error:
        print(json.dumps({"status": "blocked", "error": str(error)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
