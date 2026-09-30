"""MCP起動管理の排他・所有権・停止確認を決定的なモックで検証する。"""

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
REAL_POPEN = subprocess.Popen
BRIDGE = ROOT / '.orchestration/mcp-launcher-bridge'
sys.path.insert(0, str(BRIDGE))
if (BRIDGE / 'launcher_core.py').exists():
    import launcher_core as core
else:
    core = None


class ManagerTest(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(core, 'JobManager は未実装')
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / '.orchestration')
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name)
        self.manager = core.JobManager(threading.Event(), workspace_root=self.workspace)
        self.process = mock.Mock(pid=999999)
        self.process.poll.return_value = None
        self.process.wait.return_value = 0
        for name, value in [('command_for', ['dummy']), ('collect_result', None),
                            ('stop_process_group', 0)]:
            patch = mock.patch.object(core, name, return_value=value, create=True)
            setattr(self, name, patch.start())
            self.addCleanup(patch.stop)
        patch = mock.patch.object(core.subprocess, 'Popen', return_value=self.process)
        self.popen = patch.start()
        self.addCleanup(patch.stop)
        patch = mock.patch.object(core.JobManager, '_start_monitor')
        patch.start()
        self.addCleanup(patch.stop)
        self.addCleanup(self.manager.shutdown_and_wait_all)

    def launch(self, **kwargs):
        args = dict(task_id='task-1', harness='codex', role='reviewer',
                    workspace=str(self.workspace), prompt='Review this', cancel_grace=0.02)
        args.update(kwargs)
        return self.manager.launch(**args)

    def status(self, job_id):
        return self.manager.status('task-1', str(self.workspace), job_id)

    def cancel(self, job_id):
        return self.manager.cancel('task-1', str(self.workspace), job_id)

    def job(self):
        response = self.launch()
        return self.manager.registry[response['job_id']]

    def foreign(self, status='running'):
        state = dict(job_id='old-job', task_id='task-1', harness='codex', status=status,
                     exit_code=None, error=None, reason=None, response_path=None,
                     started_at=1.0, finished_at=None)
        path = self.workspace / '.orchestration/task-1/jobs/old-job/state.json'
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(state))
        return path, state

    def test_duplicate_launch_returns_existing_job_envelope(self):
        first = self.launch()
        self.assertEqual(self.launch(), first)
        self.popen.assert_called_once()

    def test_unresolved_foreign_job_blocks_new_launch(self):
        path, previous = self.foreign()
        before = path.read_bytes()
        response = self.launch()
        self.assertEqual(response['status'], 'blocked')
        self.assertEqual(response['job_id'], previous['job_id'])
        self.assertTrue(response['error'])
        self.assertEqual(path.read_bytes(), before)
        self.popen.assert_not_called()

    def test_blocked_response_returns_existing_job_id_and_common_envelope(self):
        _, previous = self.foreign()
        response = self.launch()
        self.assertTrue(set(previous) <= response.keys())
        self.assertEqual(response['job_id'], 'old-job')
        self.assertEqual(response['status'], 'blocked')

    def test_status_on_foreign_job_is_readonly_and_reports_unknown(self):
        path, state = self.foreign()
        before = path.read_bytes()
        self.assertEqual(self.status(state['job_id'])['status'], 'unknown')
        self.assertEqual(path.read_bytes(), before)

    def test_status_on_foreign_terminal_job_returns_file_content_verbatim(self):
        path, state = self.foreign('completed')
        before = path.read_bytes()
        self.assertEqual(self.status(state['job_id']), state)
        self.assertEqual(path.read_bytes(), before)

    def test_cancel_on_foreign_job_raises_tool_error_without_writing(self):
        path, state = self.foreign()
        before = path.read_bytes()
        with self.assertRaisesRegex(ValueError, 'owner|オーナー'):
            self.cancel(state['job_id'])
        self.assertEqual(path.read_bytes(), before)

    def test_unknown_job_id_raises_tool_error(self):
        for action in (self.status, self.cancel):
            with self.assertRaisesRegex(ValueError, 'unknown job_id'):
                action('missing')

    def test_workspace_boundary_and_symlink_are_rejected(self):
        with self.assertRaises(ValueError):
            self.launch(workspace=str(self.workspace.parent))
        (self.workspace / '.orchestration').symlink_to(self.workspace.parent, target_is_directory=True)
        with self.assertRaises(ValueError):
            self.launch()
        self.popen.assert_not_called()

    def test_parent_blocked_rejects_launch(self):
        task = self.workspace / '.orchestration/task-1'
        task.mkdir(parents=True)
        (task / 'state.json').write_text('{"phase":"blocked"}')
        with self.assertRaisesRegex(ValueError, 'blocked'):
            self.launch()
        self.popen.assert_not_called()

    def test_launch_rejected_after_shutdown_event_set(self):
        self.manager.shutdown_event.set()
        with self.assertRaisesRegex(ValueError, 'shutdown'):
            self.launch()
        self.popen.assert_not_called()

    def run_threads(self, *actions):
        errors = []
        def wrapped(action):
            try:
                action()
            except BaseException as error:
                errors.append(error)
        threads = [threading.Thread(target=wrapped, args=(action,)) for action in actions]
        for thread in threads:
            thread.start()
        return threads, errors

    def finish_threads(self, threads, errors):
        for thread in threads:
            thread.join(3)
            self.assertFalse(thread.is_alive(), '競合テストが期限内に終了しない')
        self.assertEqual(errors, [])

    def race_starting(self, fail=False):
        entered, release, cancelled = threading.Event(), threading.Event(), threading.Event()
        def popen(*args, **kwargs):
            entered.set()
            self.assertTrue(release.wait(2))
            if fail:
                raise OSError('Popen failed')
            return self.process
        self.popen.side_effect = popen
        threads, errors = self.run_threads(self.launch)
        self.assertTrue(entered.wait(2))
        job_id = next(iter(self.manager.registry))
        more, more_errors = self.run_threads(lambda: (self.cancel(job_id), cancelled.set()))
        self.assertFalse(cancelled.wait(0.05))
        release.set()
        self.finish_threads(threads, errors)
        self.finish_threads(more, more_errors)
        self.assertTrue(cancelled.is_set())
        self.assertEqual(self.status(job_id)['status'], 'failed' if fail else 'running')

    def test_cancel_during_starting_cannot_race_ahead_of_popen(self):
        self.race_starting()

    def test_launch_lock_held_through_popen_failure(self):
        self.race_starting(fail=True)

    def test_launch_rechecks_shutdown_event_after_lock_acquisition_and_is_not_orphaned(self):
        attempted = threading.Event()
        original = self.manager._validate_launch
        def validate(*args, **kwargs):
            value = original(*args, **kwargs)
            attempted.set()
            return value
        with self.manager.task_lock, mock.patch.object(self.manager, '_validate_launch', side_effect=validate):
            threads, errors = self.run_threads(self.launch)
            self.assertTrue(attempted.wait(2))
            self.manager.shutdown_and_wait_all()
        for thread in threads:
            thread.join(2)
            self.assertFalse(thread.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertIn('shutdown', str(errors[0]))
        self.assertEqual(self.manager.registry, {})
        self.popen.assert_not_called()

    def test_job_started_during_shutdown_race_is_not_orphaned(self):
        entered, release = threading.Event(), threading.Event()
        def popen(*args, **kwargs):
            entered.set()
            self.assertTrue(release.wait(2))
            return self.process
        self.popen.side_effect = popen
        threads, errors = self.run_threads(self.launch)
        self.assertTrue(entered.wait(2))
        more, more_errors = self.run_threads(self.manager.shutdown_and_wait_all)
        self.assertTrue(self.manager.shutdown_event.wait(2))
        release.set()
        self.finish_threads(threads, errors)
        self.finish_threads(more, more_errors)
        job = next(iter(self.manager.registry.values()))
        self.assertEqual(job.state['status'], 'cancelled')
        self.assertEqual(job.state['reason'], 'shutdown')
        self.assertEqual(job.exit_code, 0)

    def test_ensure_stopped_is_idempotent_across_concurrent_callers(self):
        job = self.job()
        threads, errors = self.run_threads(*[
            lambda: self.manager.ensure_stopped(job.state['job_id'], 'user_requested') for _ in range(4)])
        self.finish_threads(threads, errors)
        self.stop_process_group.assert_called_once()
        self.assertEqual(job.state['status'], 'cancelled')
        self.assertTrue(job.state['stop_confirmed'])

    def test_shutdown_and_wait_all_blocks_until_every_job_exit_code_is_captured(self):
        jobs = [self.job()]
        response = self.launch(task_id='task-2')
        jobs.append(self.manager.registry[response['job_id']])
        entered, release, done = threading.Event(), threading.Event(), threading.Event()
        def stop(*args):
            entered.set()
            self.assertTrue(release.wait(2))
            return -15
        self.stop_process_group.side_effect = stop
        threads, errors = self.run_threads(lambda: (self.manager.shutdown_and_wait_all(), done.set()))
        self.assertTrue(entered.wait(2))
        self.assertFalse(done.is_set())
        release.set()
        self.finish_threads(threads, errors)
        self.assertTrue(all(job.exit_code == -15 for job in jobs))

    def test_cancel_after_natural_completion_is_noop(self):
        job = self.job()
        self.manager.ensure_stopped(job.state['job_id'], 'natural')
        before = dict(job.state)
        saved = (job.directory / 'state.json').read_bytes()
        self.assertEqual(before['status'], 'completed')
        self.assertEqual(self.cancel(job.state['job_id']), before)
        self.assertFalse(job.cancel_event.is_set())
        self.assertEqual((job.directory / 'state.json').read_bytes(), saved)

    def test_cancel_before_terminal_lock_is_reflected_in_final_state(self):
        job = self.job()
        entered, release = threading.Event(), threading.Event()
        original_lock = job.lock
        stopper = []

        def stop():
            stopper.append(threading.get_ident())
            self.manager.ensure_stopped(job.state['job_id'], 'natural')

        test = self

        class LockGate:
            def __enter__(self):
                # 停止側だけを、終端確定ロックの取得直前で止める。
                if threading.get_ident() == stopper[0] and job.state['status'] == 'stopping':
                    entered.set()
                    test.assertTrue(release.wait(3))
                return original_lock.__enter__()

            def __exit__(self, *args):
                return original_lock.__exit__(*args)

        with mock.patch.object(job, 'lock', LockGate()):
            threads, errors = self.run_threads(stop)
            try:
                self.assertTrue(entered.wait(2))
                self.assertEqual(self.cancel(job.state['job_id'])['status'], 'stopping')
                self.assertTrue(job.cancel_event.is_set())
            finally:
                release.set()
                self.finish_threads(threads, errors)
        self.assertEqual(job.state['status'], 'cancelled')
        self.assertEqual(job.state['reason'], 'user_requested')
        self.assertTrue(job.state['stop_confirmed'])
        self.assertEqual(json.loads((job.directory / 'state.json').read_text()), job.state)

    def test_cancel_waiting_for_terminal_lock_does_not_set_event(self):
        job = self.job()
        evaluated, release, attempted = threading.Event(), threading.Event(), threading.Event()
        original_lock, original_reason = job.lock, self.manager._reason
        responses, calls = [], []

        def reason(*args):
            result = original_reason(*args)
            calls.append(result)
            if len(calls) == 2:
                evaluated.set()
                self.assertTrue(release.wait(3))
            return result

        class ObservedLock:
            def __enter__(self):
                if evaluated.is_set():
                    attempted.set()
                return original_lock.__enter__()

            def __exit__(self, *args):
                return original_lock.__exit__(*args)

        groups = []
        with mock.patch.object(job, 'lock', ObservedLock()), \
             mock.patch.object(self.manager, '_reason', side_effect=reason):
            try:
                groups.append(self.run_threads(lambda: self.manager.ensure_stopped(job.state['job_id'], 'natural')))
                self.assertTrue(evaluated.wait(2))
                groups.append(self.run_threads(lambda: responses.append(self.cancel(job.state['job_id']))))
                self.assertTrue(attempted.wait(2))
                self.assertEqual(job.state['status'], 'stopping')
                self.assertFalse(job.cancel_event.is_set(), '終端確定中にロック外でcancelが受け付けられた')
            finally:
                release.set()
                for threads, errors in groups:
                    self.finish_threads(threads, errors)
        self.assertEqual(job.state['status'], 'completed')
        self.assertIsNone(job.state['reason'])
        self.assertFalse(job.cancel_event.is_set())
        self.assertEqual(responses, [job.state])
        self.assertEqual(json.loads((job.directory / 'state.json').read_text()), job.state)

    def check_shutdown_state_write_failure(self, first_code, failed_status):
        first = self.job()
        response = self.launch(task_id='task-2')
        second = self.manager.registry[response['job_id']]
        second.process = mock.Mock(pid=999998)
        write = core.write_json
        failed_writes = []

        def fail_first_final_state(path, state):
            if path.parent == first.directory and state['status'] == failed_status:
                failed_writes.append(dict(state))
                raise OSError('simulated final state write failure')
            write(path, state)

        self.stop_process_group.side_effect = lambda process, grace: first_code if process is first.process else -15
        with mock.patch.object(core, 'write_json', side_effect=fail_first_final_state):
            threads, errors = self.run_threads(self.manager.shutdown_and_wait_all)
            self.finish_threads(threads, errors)
        self.assertEqual(len(failed_writes), 1)
        self.assertIn('final state write failure', first.failure)
        self.assertEqual(first.state['status'], failed_status)
        self.assertEqual(first.exit_code, first_code)
        self.assertEqual(first.state['stop_confirmed'], first_code is not None)
        self.stop_process_group.assert_has_calls([
            mock.call(first.process, first.cancel_grace),
            mock.call(second.process, second.cancel_grace),
        ])
        self.assertEqual(second.state['status'], 'cancelled')
        self.assertEqual(second.exit_code, -15)
        self.assertTrue(second.state['stop_confirmed'])
        # unknown の後片付けでは、次の停止確認を成功させる。
        self.stop_process_group.side_effect = None

    def test_shutdown_continues_after_unknown_state_write_failure(self):
        self.check_shutdown_state_write_failure(None, 'unknown')

    def test_shutdown_continues_after_terminal_state_write_failure(self):
        self.check_shutdown_state_write_failure(-15, 'cancelled')

    def test_other_job_operations_respond_while_stopping_job_is_queried(self):
        for first_action in ('status', 'cancel'):
            for second_action in ('status', 'cancel'):
                with self.subTest(first_action=first_action, second_action=second_action):
                    first_task = f'{first_action}-{second_action}-first'
                    first = self.launch(task_id=first_task)
                    second_task = f'{first_action}-{second_action}-second'
                    second = self.launch(task_id=second_task)
                    entered, release = threading.Event(), threading.Event()
                    queried, responded = threading.Event(), threading.Event()
                    owned = self.manager._owned

                    def stop(*args):
                        entered.set()
                        self.assertTrue(release.wait(3))
                        return 0

                    def lookup(*args):
                        result = owned(*args)
                        if args[2] == first['job_id']:
                            queried.set()
                        return result

                    def other_operation():
                        result = getattr(self.manager, second_action)(
                            second_task, str(self.workspace), second['job_id'])
                        self.assertEqual(result['status'], 'running')
                        responded.set()

                    self.stop_process_group.side_effect = stop
                    groups = []
                    with mock.patch.object(self.manager, '_owned', side_effect=lookup):
                        try:
                            groups.append(self.run_threads(lambda: self.manager.ensure_stopped(first['job_id'], 'natural')))
                            self.assertTrue(entered.wait(2))
                            groups.append(self.run_threads(lambda: getattr(self.manager, first_action)(
                                first_task, str(self.workspace), first['job_id'])))
                            self.assertTrue(queried.wait(2))
                            groups.append(self.run_threads(other_operation))
                            self.assertTrue(responded.wait(0.5), '別jobの操作が停止待ちに巻き込まれた')
                        finally:
                            release.set()
                            for threads, errors in groups:
                                self.finish_threads(threads, errors)

    def check_cancel_during_cleanup(self, phase):
        job = self.job()
        entered, release, responded = threading.Event(), threading.Event(), threading.Event()

        def pause(*args):
            entered.set()
            self.assertTrue(release.wait(3))
            return 0

        getattr(self, phase).side_effect = pause
        groups = []
        responses = []

        def cancel():
            responses.append(self.cancel(job.state['job_id']))
            responded.set()

        try:
            groups.append(self.run_threads(lambda: self.manager.ensure_stopped(job.state['job_id'], 'natural')))
            self.assertTrue(entered.wait(2))
            groups.append(self.run_threads(cancel))
            self.assertTrue(job.cancel_event.wait(0.5), '停止途中のcancelが受け付けられない')
            self.assertTrue(responded.wait(0.5), 'cancelが停止完了を待っている')
            self.assertEqual(responses[0]['status'], 'stopping')
        finally:
            release.set()
            for threads, errors in groups:
                self.finish_threads(threads, errors)
        self.assertEqual(job.state['status'], 'cancelled')
        self.assertEqual(job.state['reason'], 'user_requested')
        self.assertTrue(job.state['stop_confirmed'])

    def test_snapshot_wait_does_not_hold_task_lock(self):
        job = self.job()
        for action in ('status', 'cancel'):
            with self.subTest(action=action):
                queried = threading.Event()
                owned = self.manager._owned

                def lookup(*args):
                    result = owned(*args)
                    queried.set()
                    return result

                with mock.patch.object(self.manager, '_owned', side_effect=lookup):
                    try:
                        with job.lock:
                            threads, errors = self.run_threads(lambda: getattr(self, action)(job.state['job_id']))
                            self.assertTrue(queried.wait(2))
                            acquired = self.manager.task_lock.acquire(timeout=0.5)
                            if acquired:
                                self.manager.task_lock.release()
                            self.assertTrue(acquired, '状態スナップショット待ちがtask_lockを保持している')
                    finally:
                        self.finish_threads(threads, errors)
                if action == 'cancel':
                    self.assertTrue(job.cancel_event.is_set())

    def test_cancel_during_process_stop_is_accepted_before_terminal_state(self):
        self.check_cancel_during_cleanup('stop_process_group')

    def test_cancel_during_result_collection_is_accepted_before_terminal_state(self):
        self.check_cancel_during_cleanup('collect_result')

    def test_cancel_wins_over_timeout_in_same_tick(self):
        job = self.job()
        job.deadline = 0
        job.cancel_event.set()
        self.manager._monitor(job)
        self.assertEqual(job.state['status'], 'cancelled')
        self.assertEqual(job.state['reason'], 'user_requested')

    def test_shutdown_event_preempts_cancel_and_timeout(self):
        job = self.job()
        job.deadline = 0
        job.cancel_event.set()
        self.manager.shutdown_event.set()
        self.manager._monitor(job)
        self.assertEqual(job.state['reason'], 'shutdown')

    def test_shutdown_cancelled_job_has_reason_shutdown_field(self):
        job = self.job()
        self.manager.shutdown_and_wait_all()
        self.assertEqual(job.state['status'], 'cancelled')
        self.assertEqual(job.state['reason'], 'shutdown')

    def test_timeout(self):
        job = self.job()
        job.deadline = 0
        self.manager._monitor(job)
        self.assertEqual(job.state['status'], 'timed_out')
        self.assertEqual(job.state['reason'], 'timeout')

    def test_interrupt_during_cleanup_is_not_success(self):
        job = self.job()
        def stop(*args):
            self.manager.shutdown_event.set()
            return 0
        self.stop_process_group.side_effect = stop
        self.manager.ensure_stopped(job.state['job_id'], 'natural')
        self.assertEqual(job.state['status'], 'cancelled')
        self.assertEqual(job.state['reason'], 'shutdown')

    def test_state_write_failure_still_cleans_exited_child_group(self):
        write = core.write_json
        def fail_running(path, state):
            if state['status'] == 'running':
                raise OSError('simulated state write failure')
            write(path, state)
        self.process.poll.return_value = 0
        with mock.patch.object(core, 'write_json', side_effect=fail_running):
            response = self.launch()
        self.stop_process_group.assert_called_once_with(self.process, 0.02)
        self.assertEqual(response['status'], 'failed')
        self.assertIn('state write failure', response['error'])


class StopGroupTest(ManagerTest):
    # 継承元の管理テストは ManagerTest のみで収集する。
    def test_ensure_stopped_waits_for_descendant_group_even_if_leader_already_exited(self):
        self.check_group_confirmation(survives=False)

    def test_ensure_stopped_does_not_finalize_state_before_group_confirmed_dead(self):
        self.check_group_confirmation(survives=False)

    def test_ensure_stopped_marks_unknown_when_group_survives_kill_confirmation(self):
        self.check_group_confirmation(survives=True)

    def test_transient_permission_error_is_rechecked_within_short_grace(self):
        clock = [0.0]
        signals = []
        self.process.poll.side_effect = [None, -signal.SIGTERM]

        def killpg(pid, sig):
            signals.append(sig)
            if sig == 0:
                if clock[0] < 0.02:
                    raise PermissionError(1, 'Operation not permitted')
                raise ProcessLookupError

        def sleep(seconds):
            self.assertLessEqual(seconds, 0.02)
            clock[0] += seconds

        with mock.patch.object(core.os, 'killpg', side_effect=killpg), \
             mock.patch.object(core.time, 'monotonic', side_effect=lambda: clock[0]), \
             mock.patch.object(core.time, 'sleep', side_effect=sleep):
            self.assertEqual(REAL_STOP(self.process, 0.02), -signal.SIGTERM)
        self.assertNotIn(signal.SIGKILL, signals)
        self.process.wait.assert_not_called()

    def test_persistent_probe_permission_error_does_not_confirm_group_death(self):
        job = self.job()
        clock, signals = [0.0], []
        self.process.poll.return_value = 0

        def killpg(pid, sig):
            signals.append(sig)
            if sig == 0:
                raise PermissionError(1, 'Operation not permitted')

        def sleep(seconds):
            clock[0] += seconds

        with mock.patch.object(core, 'stop_process_group', REAL_STOP), \
             mock.patch.object(core.os, 'killpg', side_effect=killpg), \
             mock.patch.object(core.time, 'monotonic', side_effect=lambda: clock[0]), \
             mock.patch.object(core.time, 'sleep', side_effect=sleep):
            self.manager.ensure_stopped(job.state['job_id'], 'user_requested')
        self.assertIn(signal.SIGKILL, signals)
        self.assertGreaterEqual(clock[0], job.cancel_grace + core.KILL_CONFIRM_TIMEOUT)
        self.assertEqual(job.state['status'], 'unknown')
        self.assertFalse(job.state['stop_confirmed'])
        self.assertIsNone(job.exit_code)
        self.process.wait.assert_not_called()

    def check_group_confirmation(self, survives):
        job = self.job()
        clock, signals = [0.0], []
        kill_sent, probes_after_kill = [False], [0]
        self.process.poll.return_value = 0
        def killpg(pid, sig):
            signals.append(sig)
            self.assertIsNone(job.exit_code)
            self.assertNotIn(job.state['status'], core.TERMINAL)
            if sig == signal.SIGKILL:
                kill_sent[0] = True
            elif sig == 0 and kill_sent[0]:
                probes_after_kill[0] += 1
                if not survives and probes_after_kill[0] >= 3:
                    raise ProcessLookupError
        def sleep(seconds):
            clock[0] += seconds
        with mock.patch.object(core, 'stop_process_group', REAL_STOP), \
             mock.patch.object(core.os, 'killpg', side_effect=killpg), \
             mock.patch.object(core.time, 'monotonic', side_effect=lambda: clock[0]), \
             mock.patch.object(core.time, 'sleep', side_effect=sleep):
            self.manager.ensure_stopped(job.state['job_id'], 'user_requested')
        self.assertEqual(signals[0], signal.SIGTERM)
        self.assertIn(signal.SIGKILL, signals)
        self.assertGreaterEqual(probes_after_kill[0], 3)
        self.assertGreaterEqual(self.process.poll.call_count, probes_after_kill[0])
        self.process.wait.assert_not_called()
        if survives:
            self.assertEqual(job.state['status'], 'unknown')
            self.assertIsNone(job.exit_code)
            self.assertFalse(job.state['stop_confirmed'])
            state = json.loads((job.directory / 'state.json').read_text())
            self.assertEqual(state['status'], 'unknown')
            self.assertFalse(state['stop_confirmed'])
            # 未確定の場合は次回の停止確認を許す。
            self.manager.ensure_stopped(job.state['job_id'], 'user_requested')
            self.assertEqual(job.state['status'], 'cancelled')
        else:
            self.assertEqual(job.exit_code, 0)
            self.assertEqual(job.state['status'], 'cancelled')
            self.assertTrue(job.state['stop_confirmed'])

    def test_ensure_stopped_reaps_leader_without_zombie_and_confirms_cancelled_when_no_descendants(self):
        # 実OSのスケジューリングには、モック用20msではなく複数回の確認献予を与える。
        response = self.launch(cancel_grace=1)
        job = self.manager.registry[response['job_id']]
        process = REAL_POPEN([sys.executable, '-c', 'import time; time.sleep(30)'],
                             start_new_session=True)
        self.addCleanup(lambda: process.poll() is None and (process.kill(), process.wait()))
        job.process = process
        original_killpg = os.killpg
        with mock.patch.object(core, 'stop_process_group', REAL_STOP), \
             mock.patch.object(core.os, 'killpg', wraps=original_killpg) as killpg, \
             mock.patch.object(process, 'poll', wraps=process.poll) as poll:
            self.manager.ensure_stopped(job.state['job_id'], 'user_requested')
        self.assertGreater(poll.call_count, 0)
        self.assertNotIn(mock.call(process.pid, signal.SIGKILL), killpg.call_args_list)
        self.assertEqual(job.state['status'], 'cancelled')
        self.assertTrue(job.state['stop_confirmed'])
        with self.assertRaises(ProcessLookupError):
            os.killpg(process.pid, 0)


# テストヘルパーを共有しつつ継承による二重実行を避ける。
for _name in dir(ManagerTest):
    if _name.startswith('test_'):
        setattr(StopGroupTest, _name, None)
REAL_STOP = getattr(core, 'stop_process_group', None)


if __name__ == '__main__':
    unittest.main()


class HarnessPortTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / '.orchestration')
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name)
        self.manager = core.JobManager(threading.Event(), workspace_root=self.workspace)
        self.process = mock.Mock(pid=999999)
        self.process.poll.return_value = 0
        self.process.wait.return_value = 0
        self.process.returncode = 0
        self.code = 0
        def popen(command, cwd, stdin, stdout, stderr, start_new_session):
            request = stdin.read()
            self.assertTrue(start_new_session)
            if '--output-last-message' in command:
                path = Path(command[command.index('--output-last-message') + 1])
                path.write_text('' if 'EMPTY' in request else 'FAKE_OK')
                stdout.write('{"type":"item.completed"}\n')
            else:
                stdout.write(json.dumps(dict(result='FAKE_OK', is_error='JSON_ERROR' in request)))
            stdout.flush()
            return self.process
        for target, kwargs in [('Popen', dict(side_effect=popen))]:
            patch = mock.patch.object(core.subprocess, target, **kwargs)
            self.popen = patch.start()
            self.addCleanup(patch.stop)
        patch = mock.patch.object(core, 'stop_process_group', side_effect=lambda *args: self.code)
        patch.start()
        self.addCleanup(patch.stop)
        patch = mock.patch.object(core.JobManager, '_start_monitor')
        patch.start()
        self.addCleanup(patch.stop)
        self.addCleanup(self.manager.shutdown_and_wait_all)

    def launch(self, **kwargs):
        args = dict(task_id='task-1', harness='codex', role='reviewer', workspace=str(self.workspace),
                    prompt='Review the task', executable_override='dummy')
        args.update(kwargs)
        response = self.manager.launch(**args)
        if response['status'] == 'running':
            self.manager.ensure_stopped(response['job_id'], 'natural')
        return self.manager.status(args['task_id'], args['workspace'], response['job_id'])

    def test_success_and_skill_injection(self):
        state = self.launch()
        self.assertEqual(state['status'], 'completed')
        path = Path(state['response_path'])
        self.assertEqual(path.read_text(), 'FAKE_OK')
        request = (path.parent / 'request.md').read_text()
        self.assertIn('# Reviewer', request)
        self.assertIn('再委任は禁止', request)
        self.assertIn('Review the task', request)

    def test_model_and_reasoning_effort_are_translated_per_harness(self):
        for harness in ('codex', 'claude'):
            command = core.command_for(harness, harness, self.workspace, self.workspace,
                                       'workspace-write', 'test-model', 5, 'high', 'coder')
            self.assertEqual(command[command.index('--model') + 1], 'test-model')
            if harness == 'codex':
                self.assertIn('model_reasoning_effort="high"', command)
                self.assertIn('--dangerously-bypass-approvals-and-sandbox', command)
                self.assertNotIn('--sandbox', command)
            else:
                self.assertEqual(command[command.index('--effort') + 1], 'high')
                self.assertEqual(command[command.index('--permission-mode') + 1], 'acceptEdits')
                self.assertEqual(command[command.index('--disallowedTools') + 1], 'Agent,Task')

    def test_none_model_and_reasoning_effort_are_omitted_per_harness(self):
        for harness in ('codex', 'claude'):
            command = core.command_for(harness, harness, self.workspace, self.workspace,
                                      'read-only', None, 5, None, 'reviewer')
            self.assertNotIn('--model', command)
            self.assertNotIn('--effort', command)
            self.assertNotIn('model_reasoning_effort', ' '.join(command))
            if harness == 'claude':
                self.assertEqual(command[command.index('--tools') + 1], 'Read,Glob,Grep')

    def test_claude_json_error_is_not_success(self):
        self.assertEqual(self.launch(harness='claude', prompt='JSON_ERROR')['status'], 'failed')

    def test_claude_success(self):
        self.assertEqual(self.launch(harness='claude')['status'], 'completed')

    def test_packet_is_injected_into_request(self):
        packet = self.workspace / 'packet'
        packet.mkdir()
        (packet / 'manifest.json').write_text('{"files":["issue.md"]}')
        (packet / 'issue.md').write_text('ISSUE_BODY')
        state = self.launch(packet=str(packet))
        self.assertEqual(state['status'], 'completed')
        self.assertIn('ISSUE_BODY', (Path(state['response_path']).parent / 'request.md').read_text())

    def test_packet_rejects_symlink_and_outside_workspace(self):
        packet = self.workspace / 'packet'
        packet.mkdir()
        (packet / 'manifest.json').write_text('{"files":["linked.md"]}')
        (packet / 'linked.md').symlink_to(ROOT / 'skills/reviewer/SKILL.md')
        link = self.workspace / 'linked-packet'
        link.symlink_to(packet, target_is_directory=True)
        for path in (packet, self.workspace.parent, link):
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.launch(packet=str(path))
        self.popen.assert_not_called()

    def test_cli_failure_is_reported(self):
        self.code = 17
        state = self.launch()
        self.assertEqual(state['status'], 'failed')
        self.assertEqual(state['exit_code'], 17)
        self.assertIn('17', state['error'])

    def test_empty_final_response_is_not_success(self):
        self.assertEqual(self.launch(prompt='EMPTY')['status'], 'failed')

    def test_claude_denials_malformed_and_non_string_results_fail(self):
        for content in ('not-json', '[]', '{"result":2}',
                        '{"result":"OK","permission_denials":["Write"]}', '{"result":" "}'):
            (self.workspace / 'stdout.log').write_text(content)
            with self.subTest(content=content), self.assertRaises(ValueError):
                core.collect_result('claude', self.workspace)
