"""claude/codex の起動・所有権・停止確認を管理する共通基盤。"""

from contextlib import contextmanager
from dataclasses import dataclass, field
import fcntl
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import threading
import time
import uuid

TERMINAL = {'completed', 'failed', 'timed_out', 'cancelled'}
_POLL_INTERVAL = 0.05
KILL_CONFIRM_TIMEOUT = 2
SKILLS = Path(__file__).resolve().parents[2] / 'skills'


def write_json(path, value):
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temporary.replace(path)


def stop_process_group(process, grace=1):
    """代表を随時回収し、グループ消滅を確認した場合のみ終了コードを返す。"""
    leader_returncode = None

    def confirmed():
        nonlocal leader_returncode
        code = process.poll()
        if code is not None and leader_returncode is None:
            leader_returncode = code
        try:
            os.killpg(process.pid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            # 終了途中のEPERMも消滅の証拠ではない。期限内で再確認する。
            return False
        return False

    def result():
        return process.wait() if leader_returncode is None else leader_returncode

    def wait_for_group(timeout):
        deadline = time.monotonic() + timeout
        while True:
            if confirmed():
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            # 献予が確認間隔より短くても、期限で再確認してから昇格する。
            time.sleep(min(_POLL_INTERVAL, remaining))

    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return result()
    if wait_for_group(grace):
        return result()
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        return result()
    if wait_for_group(KILL_CONFIRM_TIMEOUT):
        return result()
    # シグナル送信だけでは停止済みと見なさない。
    return None


PACKET_MAX_FILE_BYTES = 256 * 1024
PACKET_MAX_TOTAL_BYTES = 2 * 1024 * 1024


def packet_file(path):
    """リンクを辿らず packet の通常ファイルを上限付きで読む。"""
    info = os.lstat(path)
    if not path.is_file() or path.is_symlink():
        raise ValueError(f"packet のファイルはリンクでない通常ファイルである必要があります: {path}")
    if info.st_size > PACKET_MAX_FILE_BYTES:
        raise ValueError(f"packet のファイルは {PACKET_MAX_FILE_BYTES // 1024} KiB 以下にしてください: {path}")
    return path.read_text(encoding="utf-8")


def packet_text(workspace, packet):
    """manifest が列挙する workspace 内 packet だけを request に埋め込む。"""
    if packet is None:
        return ""
    try:
        packet = Path(os.path.abspath(packet))
        packet.stat()
    except OSError as error:
        raise ValueError(f"packet を解決できません: {error}") from error
    try:
        relative = packet.relative_to(workspace)
    except ValueError as error:
        raise ValueError("packet は workspace 配下で指定してください") from error
    current = workspace
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise ValueError(f"packet のディレクトリにリンクは使えません: {current}")
    if not packet.is_dir():
        raise ValueError("packet は実ディレクトリで指定してください")
    manifest_path = packet / "manifest.json"
    manifest_text = packet_file(manifest_path)
    try:
        manifest = json.loads(manifest_text)
    except json.JSONDecodeError as error:
        raise ValueError(f"packet manifest は JSON object である必要があります: {error}") from error
    files = manifest.get("files") if isinstance(manifest, dict) and set(manifest) == {"files"} else None
    if not isinstance(files, list) or not files or any(not isinstance(name, str) for name in files):
        raise ValueError("packet manifest は空でない files 配列だけを持つ必要があります")
    if len(files) != len(set(files)):
        raise ValueError("packet manifest の files は重複できません")
    sections = []
    total = 0
    for name in files:
        entry = Path(name)
        if entry.is_absolute() or not name or any(part in {"", ".", ".."} for part in entry.parts):
            raise ValueError(f"packet manifest のファイル名が不正です: {name}")
        path = packet / entry
        try:
            path.relative_to(packet)
        except ValueError as error:
            raise ValueError(f"packet manifest のファイル名が不正です: {name}") from error
        current = packet
        for part in entry.parts:
            current /= part
            if current.is_symlink():
                raise ValueError(f"packet のファイルにリンクは使えません: {current}")
        text = packet_file(path)
        total += len(text.encode("utf-8"))
        if total > PACKET_MAX_TOTAL_BYTES:
            raise ValueError(f"packet の合計サイズは {PACKET_MAX_TOTAL_BYTES // (1024 * 1024)} MiB 以下にしてください")
        sections.append(f"### {name}\n\n{text}")
    return "\n\n## Packet\n\n" + "\n\n".join(sections) + "\n"


def command_for(harness, executable, workspace, job, access, model, timeout,
                reasoning_effort=None, role=None):
    if harness == "codex":
        # 注記: codexのネストサンドボックス(終了コード71)を回避するため、
        # codex自身のサンドボックス機構(--sandbox/approval_policy)は使用しない。
        # read-only強制などのアクセス制御は、この呼び出し元である
        # Bash tool側の親Seatbelt(サンドボックス)設定に責任が移る。
        # つまり「readonlyだから安全」という前提は、親サンドボックスが
        # 実際にファイルシステム書き込みを拒否していることに依存する。
        #
        # `access`引数はcodex分岐では未使用になるが、command_for()の
        # シグネチャ自体とCLIの`--access`選択肢は変更しない。
        # claude分岐とagyのrole guardは引き続き`access`を使用するため。
        command = [executable, "exec", "--json", "--color", "never", "--cd", str(workspace),
                   "--dangerously-bypass-approvals-and-sandbox",
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
    raise ValueError("harness は claude/codex のみ対応しています")


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


@dataclass
class JobEntry:
    directory: Path
    workspace: Path
    state: dict
    timeout: float
    cancel_grace: float
    process: object = None
    exit_code: object = None
    deadline: float = 0
    failure: object = None
    cancel_event: threading.Event = field(default_factory=threading.Event)
    lock: object = field(default_factory=threading.RLock)
    stop_lock: object = field(default_factory=threading.Lock)


class JobManager:
    def __init__(self, shutdown_event, workspace_root=None):
        self.shutdown_event = shutdown_event
        self.workspace_root = Path(workspace_root or Path.cwd()).resolve(strict=True)
        self.registry = {}
        self.task_lock = threading.RLock()

    def _paths(self, task_id, workspace, job_id=None):
        for value in (task_id, job_id):
            if value is not None and (not isinstance(value, str) or
                                     not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,79}', value)):
                raise ValueError('task_id/job_id が不正です')
        if not isinstance(workspace, str):
            raise ValueError('workspace は文字列で指定してください')
        root = Path(workspace).resolve(strict=True)
        if not root.is_dir() or not root.is_relative_to(self.workspace_root):
            raise ValueError('workspace 境界外です')
        task = root / '.orchestration' / task_id
        path = task / 'jobs'
        if job_id:
            path = path / job_id / 'state.json'
        current = root
        for part in path.relative_to(root).parts:
            current /= part
            if current.is_symlink():
                raise ValueError(f'管理用パスにリンクは使えません: {current}')
        return root, task, path

    def _validate_launch(self, task_id, harness, role, workspace, prompt, access,
                         model, reasoning_effort, timeout, cancel_grace, packet, executable_override):
        root, task, _ = self._paths(task_id, workspace)
        if harness not in {'claude', 'codex'} or role not in {'planner', 'coder', 'reviewer'}:
            raise ValueError('harness または role が不正です')
        if access not in {'read-only', 'workspace-write'} or not isinstance(prompt, str) or not prompt.strip():
            raise ValueError('access または prompt が不正です')
        for value in (model, reasoning_effort, packet, executable_override):
            if value is not None and not isinstance(value, str):
                raise ValueError('省略可能な引数は文字列で指定してください')
        for name, value, maximum in [('timeout', timeout, 86400), ('cancel_grace', cancel_grace, 60)]:
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 < value <= maximum:
                raise ValueError(f'{name} が不正です')
        return root, task

    @contextmanager
    def _file_lock(self, task):
        task.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock_path = task / 'launcher.lock'
        # 別サーバーの launch と、永続状態の確認・作成を直列化する。
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def launch(self, task_id, harness, role, workspace, prompt, access='read-only',
               model=None, reasoning_effort=None, timeout=600, cancel_grace=5,
               packet=None, executable_override=None):
        if self.shutdown_event.is_set():
            raise ValueError('shutdown 中は launch できません')
        root, task = self._validate_launch(task_id, harness, role, workspace, prompt, access,
                                          model, reasoning_effort, timeout, cancel_grace, packet,
                                          executable_override)
        request = self._request(task_id, role, root, prompt, packet)
        with self.task_lock:
            if self.shutdown_event.is_set():
                raise ValueError('shutdown 中は launch できません')
            with self._file_lock(task):
                parent_state = task / 'state.json'
                if parent_state.is_symlink():
                    raise ValueError('親状態にリンクは使えません')
                if parent_state.exists():
                    parent = json.loads(parent_state.read_text())
                    if parent.get('phase') == 'blocked' or parent.get('blocked_reason'):
                        raise ValueError('親タスクが blocked です')
                for job in self.registry.values():
                    with job.lock:
                        if job.workspace == root and job.state['task_id'] == task_id and job.state['status'] not in TERMINAL:
                            return dict(job.state)
                for previous in (task / 'jobs').glob('*/state.json'):
                    self._paths(task_id, workspace, previous.parent.name)
                    state = json.loads(previous.read_text())
                    if state.get('status') not in TERMINAL:
                        return dict(state, status='blocked', error='未解決の既存jobがあります')
                job_id = uuid.uuid4().hex
                directory = task / 'jobs' / job_id
                directory.mkdir(parents=True, mode=0o700)
                state = dict(job_id=job_id, task_id=task_id, harness=harness, status='starting',
                             exit_code=None, error=None, reason=None, response_path=None,
                             started_at=time.time(), finished_at=None)
                job = JobEntry(directory, root, state, timeout, cancel_grace)
                self.registry[job_id] = job
                try:
                    write_json(directory / 'state.json', state)
                    command = command_for(harness, executable_override or shutil.which(harness) or harness,
                                          root, directory, access, model, timeout, reasoning_effort, role)
                    (directory / 'request.md').write_text(request, encoding='utf-8')
                    with (directory / 'request.md').open() as source, \
                         (directory / 'stdout.log').open('w') as out, \
                         (directory / 'stderr.log').open('w') as err:
                        job.process = subprocess.Popen(command, cwd=root, stdin=source, stdout=out,
                                                       stderr=err, start_new_session=True)
                    job.deadline = time.monotonic() + timeout
                    state['status'] = 'running'
                    write_json(directory / 'state.json', state)
                except Exception as error:
                    job.failure = str(error)
                    if job.process is not None:
                        self.ensure_stopped(job_id, 'failure')
                    else:
                        state.update(status='failed', error=job.failure, finished_at=time.time())
                        write_json(directory / 'state.json', state)
                response = dict(state)
        if job.process is not None and state['status'] == 'running':
            self._start_monitor(job)
        return response

    def _request(self, task_id, role, workspace, prompt, packet):
        role_text = (SKILLS / role / 'SKILL.md').read_text(encoding='utf-8')
        packet_content = packet_text(workspace, Path(packet) if packet is not None else None)
        return (f'あなたは子セッションです。task_id={task_id}, role={role}。再委任は禁止。\n'
                '以下の担当スキルに従い、最後の回答に担当の結果を返してください。\n'
                '親の役割を引き受けず、commit/push/PR作成は行わないでください。\n\n'
                + role_text + '\n\n## 親からの依頼\n\n' + prompt + packet_content)

    def _owned(self, task_id, workspace, job_id):
        root, _, path = self._paths(task_id, workspace, job_id)
        job = self.registry.get(job_id)
        if job is not None and (job.workspace != root or job.state['task_id'] != task_id):
            raise ValueError('unknown job_id')
        return job, path

    def status(self, task_id, workspace, job_id):
        with self.task_lock:
            job, path = self._owned(task_id, workspace, job_id)
        if job:
            with job.lock:
                return dict(job.state)
        if not path.is_file():
            raise ValueError('unknown job_id')
        state = json.loads(path.read_text())
        if state.get('status') not in TERMINAL:
            state['status'] = 'unknown'
        return state

    def cancel(self, task_id, workspace, job_id):
        with self.task_lock:
            job, path = self._owned(task_id, workspace, job_id)
        if job is None:
            raise ValueError('非オーナーのjobはcancelできません' if path.exists() else 'unknown job_id')
        with job.lock:
            # cancel受付と終端確定を直列化し、確定済みなら要求を設定しない。
            if job.state['status'] not in TERMINAL:
                job.cancel_event.set()
            return dict(job.state)

    def _start_monitor(self, job):
        threading.Thread(target=self._monitor, args=(job,), daemon=True).start()

    def _monitor(self, job):
        while True:
            if job.cancel_event.is_set():
                reason = 'user_requested'
            elif time.monotonic() >= job.deadline:
                reason = 'timeout'
            elif job.process.poll() is not None:
                reason = 'natural'
            else:
                time.sleep(_POLL_INTERVAL)
                continue
            self.ensure_stopped(job.state['job_id'], reason)
            return

    def _reason(self, job, reason):
        if self.shutdown_event.is_set():
            return 'shutdown'
        if job.cancel_event.is_set():
            return 'user_requested'
        if time.monotonic() >= job.deadline and reason != 'failure':
            return 'timeout'
        return reason

    def ensure_stopped(self, job_id, reason):
        job = self.registry[job_id]
        # 停止処理の重複だけを直列化し、待機・結果読取中は状態参照を塞がない。
        with job.stop_lock:
            with job.lock:
                if job.exit_code is not None or job.state['status'] in TERMINAL:
                    return
                job.state['status'] = 'stopping'
                try:
                    write_json(job.directory / 'state.json', job.state)
                except OSError as error:
                    job.failure = str(error)
            try:
                code = stop_process_group(job.process, job.cancel_grace)
            except OSError as error:
                job.failure = str(error)
                code = None
            if code is None:
                with job.lock:
                    job.state.update(status='unknown', stop_confirmed=False,
                                     error=job.failure or 'プロセスグループの消滅を確認できません')
                    try:
                        write_json(job.directory / 'state.json', job.state)
                    except OSError as error:
                        job.failure = str(error)
                return
            error = job.failure
            reason = self._reason(job, reason)
            if reason == 'natural' and error is None:
                try:
                    if code:
                        raise ValueError(f'CLI が終了コード {code} を返しました')
                    collect_result(job.state['harness'], job.directory)
                except (OSError, ValueError) as failure:
                    error = str(failure)
            with job.lock:
                # 後始末・結果読取中の shutdown/cancel も終端確定前に反映する。
                reason = self._reason(job, reason)
                if reason in {'user_requested', 'shutdown'}:
                    status, public_reason = 'cancelled', reason
                elif reason == 'timeout':
                    status, public_reason = 'timed_out', reason
                else:
                    status, public_reason = ('failed' if error or code else 'completed'), None
                job.exit_code = code
                job.state.update(status=status, reason=public_reason, exit_code=code, error=error,
                                 stop_confirmed=True, finished_at=time.time(),
                                 response_path=str(job.directory / 'response.md') if status == 'completed' else None)
                try:
                    write_json(job.directory / 'state.json', job.state)
                except OSError as error:
                    job.failure = str(error)

    def shutdown_and_wait_all(self):
        self.shutdown_event.set()
        with self.task_lock:
            jobs = [job for job in self.registry.values() if job.state['status'] not in TERMINAL]
        for job in jobs:
            self.ensure_stopped(job.state['job_id'], 'shutdown')
