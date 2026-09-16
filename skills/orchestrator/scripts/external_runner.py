#!/usr/bin/env python3
"""外部ハーネスを実行し、ジョブ単位の結果・状態・取消要求を扱う。"""

import argparse
import fcntl
import importlib.util
import json
import os
import re
import shutil
import signal
import subprocess
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

SKILLS = Path(__file__).resolve().parents[2]
ACTIVE = {"starting", "running", "stopping", "unknown"}


def resolve_agy_project(workspace, explicit=None):
    """同じ scripts ディレクトリの resolver を実行形態によらず読み込む。"""
    path = Path(__file__).with_name("resolve_agy_project.py")
    spec = importlib.util.spec_from_file_location("resolve_agy_project", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.resolve_agy_project(workspace, explicit)


class AgyStream:
    """NDJSONを差分読み込みし、同一会話の終了結果だけを採用する。"""

    def __init__(self, path):
        self.path = path
        self.offset = 0
        self.conversation_id = None
        self.result = None
        self.error = None
        self.last_event = None

    def read(self, final=False):
        previous = self.offset
        with self.path.open("rb") as source:
            source.seek(self.offset)
            while True:
                line = source.readline(1024 * 1024 + 1)
                if not line:
                    break
                if len(line) > 1024 * 1024:
                    raise ValueError("agy のイベントが上限1MiBを超えました")
                if not line.endswith(b"\n") and not final:
                    break
                self.offset = source.tell()
                try:
                    event = json.loads(line)
                    if not isinstance(event, dict):
                        raise ValueError("イベントがオブジェクトではありません")
                    if self.result is not None:
                        raise ValueError("最終結果の後に追加イベントがあります")
                    name = event.get("event")
                    self.last_event = name
                    if name == "init":
                        identity = event.get("conversation_id")
                        if self.conversation_id is not None or not isinstance(identity, str) or not identity:
                            raise ValueError("init の会話IDが不正または重複しています")
                        self.conversation_id = identity
                    elif name in {"step_update", "result"}:
                        payload = event.get(name)
                        if not isinstance(payload, dict) or not self.conversation_id or payload.get("conversation_id") != self.conversation_id:
                            raise ValueError("会話IDが init と一致しません")
                        if name == "result":
                            self.result = payload
                except (ValueError, UnicodeDecodeError) as error:
                    self.error = str(error)
        return self.offset != previous

    def finish(self, job, state):
        self.read(final=True)
        state["conversation_id"] = self.conversation_id
        state["stop_confirmed"] = False
        if self.error or self.result is None:
            state.update(status="unknown", error=self.error or "agy の最終結果がありません")
            return
        outcome = self.result.get("status")
        state["harness_status"] = outcome
        write_json(job / "harness-result.json", self.result)
        response = self.result.get("response", "")
        if isinstance(response, str):
            (job / "response.md").write_text(response)
        if state.get("stop_escalated") or outcome not in {"SUCCESS", "ERROR", "CANCELED", "INTERRUPTED"}:
            state.update(status="unknown", error="agy の終了状態を確認できません")
            return
        state["stop_confirmed"] = True
        if self.result.get("denied_actions"):
            state["denied_actions"] = self.result["denied_actions"]
        requested = state.get("stop_requested")
        if requested:
            state["status"] = requested
        elif outcome in {"CANCELED", "INTERRUPTED"}:
            state["status"] = "cancelled"
        elif outcome == "SUCCESS" and state["exit_code"] == 0 and isinstance(response, str) and response.strip() and not self.result.get("denied_actions"):
            state["status"] = "completed"
        else:
            state.update(status="failed", error=self.result.get("error") or "agy の結果が成功条件を満たしません")


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


def command_for(harness, executable, workspace, job, access, model, timeout,
                reasoning_effort=None, agy_project=None):
    if harness == "codex":
        command = [executable, "exec", "--json", "--color", "never", "--cd", str(workspace),
                   "--sandbox", access, "-c", 'approval_policy="never"',
                   "--output-last-message", str(job / "response.md")]
        if model:
            command += ["--model", model]
        if reasoning_effort:
            command += ["-c", f'model_reasoning_effort="{reasoning_effort}"']
        return command + ["-"]
    if harness == "claude":
        command = [executable, "--print", "--output-format", "json",
                   "--permission-mode", "dontAsk" if access == "read-only" else "acceptEdits",
                   "--disallowedTools", "Agent,Task"]
        if access == "read-only":
            command += ["--tools", "Read,Glob,Grep"]
        if model:
            command += ["--model", model]
        if reasoning_effort:
            command += ["--effort", reasoning_effort]
        return command
    # agy の sandbox は read-only と同義ではないため明示的に区別する。
    if access == "read-only":
        raise ValueError("agy CLI に read-only 強制オプションを確認できません。workspace-write の明示が必要です")
    if agy_project is None:
        raise ValueError("agy project id could not be resolved")
    # 内部タイムアウトより先に親がSIGINTを送り、結果を読み取る時間を確保する。
    command = [executable, "--sandbox", "--input-format", "stream-json", "--output-format", "stream-json",
               "--print-timeout", f"{int(timeout) + 120}s", "--project", agy_project]
    if model:
        command += ["--model", model]
    if reasoning_effort:
        if reasoning_effort not in {"low", "medium", "high"}:
            raise ValueError("agy の reasoning-effort は low、medium、high のみ指定できます")
        command += ["--effort", reasoning_effort]
    return command


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


def interrupt_agy(process, grace, poll_stream):
    """SIGINTに対する応答と終了を待ち、強制停止の有無を返す。"""
    try:
        os.killpg(process.pid, signal.SIGINT)
    except ProcessLookupError:
        process.wait()
        return False
    deadline = time.monotonic() + grace
    while process.poll() is None and time.monotonic() < deadline:
        poll_stream()
        time.sleep(0.05)
    escalated = process.poll() is None
    stop_group(process)
    return escalated


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
    if not (job / "response.md").is_file() or not (job / "response.md").read_text().strip():
        raise ValueError("最終回答がありません")


def run(args):
    workspace = args.workspace.resolve(strict=True)
    if not workspace.is_dir() or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", args.task_id):
        raise ValueError("workspace または task-id が不正です")
    if not 0 < args.timeout <= 86400:
        raise ValueError("timeout は0秒より大きく86400秒以下で指定してください")
    grace = getattr(args, "cancel_grace", 5)
    if not 0 < grace <= 60:
        raise ValueError("cancel-grace は0秒より大きく60秒以下で指定してください")
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
        agy_project = (resolve_agy_project(workspace, getattr(args, "agy_project", None))
                       if args.harness == "agy" else None)
        command = command_for(args.harness, executable, workspace, job,
                              args.access, args.model, args.timeout,
                              args.reasoning_effort, agy_project)
        job.mkdir(mode=0o700)
        (job / "request.md").write_text(
            f"あなたは子セッションです。task_id={args.task_id}, role={args.role}。再委任は禁止。\n"
            "以下の担当スキルに従い、最後の回答に担当の結果を返してください。\n"
            "親の役割を引き受けず、commit/push/PR作成は行わないでください。\n\n"
            + role_text + "\n\n## 親からの依頼\n\n" + prompt)
        input_path = job / "request.md"
        if args.harness == "agy":
            input_path = job / "input.jsonl"
            input_path.write_text(json.dumps({"event": "user", "message": {
                "content": (job / "request.md").read_text()}}, ensure_ascii=False) + "\n")
        state = {"job": str(job), "task_id": args.task_id, "role": args.role,
                 "harness": args.harness, "transport": "external", "status": "starting",
                 "workspace": str(workspace), "access": args.access,
                 "model": args.model, "reasoning_effort": args.reasoning_effort,
                 "runner_pid": os.getpid(), "child_pid": None,
                 "started_at": time.time(), "exit_code": None}
        if agy_project is not None:
            state["agy_project"] = agy_project
        write_json(job / "state.json", state)
        print(json.dumps(state, ensure_ascii=False), flush=True)
        interrupted = False

        def interrupt(signum, frame):
            nonlocal interrupted
            interrupted = True

        handlers = {sig: signal.signal(sig, interrupt) for sig in (signal.SIGTERM, signal.SIGINT)}
        process = None
        cleaned_up = False
        stream = None

        def poll_stream():
            if stream is not None and stream.read():
                state.update(conversation_id=stream.conversation_id, last_event=stream.last_event,
                             last_event_at=time.time())
                write_json(job / "state.json", state)

        try:
            with input_path.open() as request, (job / "stdout.log").open("w") as out, (job / "stderr.log").open("w") as err:
                if args.harness == "agy":
                    stream = AgyStream(job / "stdout.log")
                if interrupted or (job / "cancel.request").exists():
                    state["status"] = "cancelled"
                else:
                    process = subprocess.Popen(command, cwd=workspace,
                                               stdin=request,
                                               stdout=out, stderr=err, start_new_session=True)
                    state.update(status="running", child_pid=process.pid)
                    write_json(job / "state.json", state)
                    deadline = time.monotonic() + args.timeout
                    while process.poll() is None:
                        poll_stream()
                        if interrupted or (job / "cancel.request").exists():
                            state["status"] = "cancelled"
                            break
                        if time.monotonic() >= deadline:
                            state["status"] = "timed_out"
                            break
                        time.sleep(0.05)
                    if stream is not None and state["status"] in {"cancelled", "timed_out"}:
                        state["stop_requested"] = state["status"]
                        state["status"] = "stopping"
                        write_json(job / "state.json", state)
                        state["stop_escalated"] = interrupt_agy(process, grace, poll_stream)
                    else:
                        # 正常終了でも残った同一グループの子プロセスを回収する。
                        stop_group(process)
                    cleaned_up = True
                    state["exit_code"] = process.returncode
                    if interrupted or (job / "cancel.request").exists():
                        state["status"] = "cancelled"
                        state["stop_requested"] = "cancelled"
            if stream is not None and process is not None:
                stream.finish(job, state)
            elif state["status"] == "running":
                if process.returncode:
                    raise ValueError(f"CLI が終了コード {process.returncode} を返しました")
                collect_result(args.harness, job)
                state["status"] = "completed"
        except Exception as error:
            state.update(status="unknown" if args.harness == "agy" and process else "failed", error=str(error))
            if args.harness == "agy":
                state["stop_confirmed"] = False
        finally:
            if process is not None and not cleaned_up:
                try:
                    stop_group(process)
                except OSError as error:
                    state.update(status="unknown", stop_confirmed=False, error=f"子の停止未確認: {error}")
            if state["status"] == "completed" and (interrupted or (job / "cancel.request").exists()):
                state["status"] = "cancelled"
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
    if state["status"] in {"starting", "running", "stopping"}:
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
    execute.add_argument("--reasoning-effort", choices=["low", "medium", "high", "xhigh", "max"])
    execute.add_argument("--agy-project")
    execute.add_argument("--timeout", type=float, default=600)
    execute.add_argument("--cancel-grace", type=float, default=5)
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
