"""実プロセスで外部委任の正常系・失敗・停止・排他を検証する。"""

import json
import importlib.util
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "skills/orchestrator/scripts/external_runner.py"
spec = importlib.util.spec_from_file_location("external_runner", RUNNER)
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


class RunnerTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name)
        bindir = self.workspace / "bin"
        bindir.mkdir()
        for harness in ("codex", "claude", "agy"):
            dest = bindir / harness
            shutil.copy2(ROOT / "tests/fixtures/fake_harness.py", dest)
            dest.chmod(0o700)
        self.env = dict(os.environ, PATH=str(bindir) + os.pathsep + os.environ["PATH"])
        self.prompt = self.workspace / "prompt.md"
        self.prompt.write_text("Check the supplied task and return a result.")

    def command(self, harness="codex", timeout="5"):
        return [sys.executable, str(RUNNER), "run", "--workspace", str(self.workspace),
                "--task-id", "task-1", "--harness", harness, "--role", "reviewer",
                "--prompt-file", str(self.prompt), "--timeout", timeout]

    def run_job(self, harness="codex", timeout="5"):
        return subprocess.run(self.command(harness, timeout), env=self.env,
                              capture_output=True, text=True, timeout=12)

    def state(self):
        states = list((self.workspace / ".orchestration/task-1/jobs").glob("*/state.json"))
        self.assertEqual(len(states), 1)
        return json.loads(states[0].read_text()), states[0].parent

    def run_cooperative_agy(self, wrong_id=False):
        """agy COOPERATIVE分岐の停止確認ロジックを、実プロセス・実SIGINT・実時間待機に
        依存せずin-processで決定的に検証するための共通ヘルパー。

        検証意図（従来のsubprocess版から変更なし）:
          - wrong_id=False: cooperative timeoutが成立した場合、stop_confirmed=True・
            status="timed_out"・conversation_id="fake-agy"・harness_status="INTERRUPTED"
            になること。
          - wrong_id=True: init と result の会話IDが不一致の場合は停止を確認できず、
            status="unknown"になること。

        実プロセスを起動して実SIGINTの配送・応答を壁時計時間で待ち合わせる方式は、
        「起動レース」（子のシグナルハンドラ登録前にSIGINTが届く）と
        「停止確認レース」（--cancel-grace内にプロセス終了を検知できない）という
        2つの実時間レースにより低確率で失敗する（Issue #1）。そのため
        subprocess.Popen / os.killpg / time.monotonic / time.sleep / stop_group を
        決定的なテストダブルに置き換え、本番の AgyStream.finish() と interrupt_agy() の
        ロジック自体はそのまま（変更・バイパスせず）実行させる。
        """
        prompt_text = "COOPERATIVE WRONG_ID" if wrong_id else "COOPERATIVE"
        self.prompt.write_text(prompt_text)
        args = type("Args", (), dict(
            workspace=self.workspace, task_id="task-1", harness="agy", role="reviewer",
            prompt_file=self.prompt, timeout=0.05, cancel_grace=0.05,
            access="workspace-write", model=None, reasoning_effort=None))()

        # 擬似クロック: 呼び出すたびに固定ステップで進む決定的な時計。
        # run()のtimeoutループ・interrupt_agyのgraceループのデッドライン判定は
        # この呼び出し回数だけで決まり、実OSスケジューリングに一切左右されない。
        clock = {"now": 0.0}

        def fake_monotonic():
            clock["now"] += 0.01
            return clock["now"]

        # 「SIGINTを受け取るまでは実行中(None)、受け取った後は終了済み」という
        # 実プロセスの挙動をそのままモデル化する。固定回数のリストで擬似するのではなく
        # SIGINT送信有無に連動させるため、ループ回数の見積もりミスによる
        # StopIterationや意図しない分岐に陥らない。
        sigint_sent = {"flag": False}

        def fake_killpg(pid, sig):
            if sig == signal.SIGINT:
                sigint_sent["flag"] = True

        def fake_poll():
            return None if not sigint_sent["flag"] else -2

        def fake_popen(command, cwd, stdin, stdout, stderr, start_new_session):
            conversation_id = "other-session" if wrong_id else "fake-agy"
            # 本番のstdout.logはテキストモード（"w"）で開かれるため、bytesではなくstrで書き込む。
            stdout.write(json.dumps({"event": "init", "conversation_id": "fake-agy"}) + "\n")
            stdout.write(json.dumps({"event": "result", "result": {
                "conversation_id": conversation_id, "status": "INTERRUPTED", "response": ""}}) + "\n")
            stdout.flush()
            process = mock.Mock(pid=999999, returncode=-2)
            process.poll.side_effect = fake_poll
            return process

        with mock.patch.dict(os.environ, self.env), \
             mock.patch.object(runner.subprocess, "Popen", side_effect=fake_popen), \
             mock.patch.object(runner.os, "killpg", side_effect=fake_killpg), \
             mock.patch.object(runner, "stop_group"), \
             mock.patch.object(runner.time, "monotonic", side_effect=fake_monotonic), \
             mock.patch.object(runner.time, "sleep", return_value=None):
            code = runner.run(args)

        self.assertNotEqual(code, 0)
        return self.state()[0]

    def test_success_and_skill_injection(self):
        result = self.run_job()
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        state, job = self.state()
        self.assertEqual(state["status"], "completed")
        self.assertEqual((job / "response.md").read_text(), "FAKE_OK")
        self.assertIn("# Reviewer", (job / "request.md").read_text())
        self.assertEqual(state["transport"], "external")

    def test_model_and_reasoning_effort_are_translated_per_harness(self):
        codex = runner.command_for("codex", "codex", self.workspace, self.workspace / "job",
                                   "workspace-write", "test-model", 5, "xhigh")
        self.assertIn("--model", codex)
        self.assertIn("test-model", codex)
        self.assertIn("-c", codex)
        self.assertIn('model_reasoning_effort="xhigh"', codex)
        self.assertNotIn("--effort", codex)
        for harness in ("claude", "agy"):
            with self.subTest(harness=harness):
                command = runner.command_for(
                    harness, harness, self.workspace, self.workspace / "job",
                    "workspace-write", "test-model", 5, "high")
                self.assertIn("--model", command)
                self.assertIn("test-model", command)
                self.assertIn("--effort", command)
                self.assertIn("high", command)

    def test_none_model_and_reasoning_effort_are_omitted_per_harness(self):
        for harness in ("codex", "claude", "agy"):
            with self.subTest(harness=harness):
                command = runner.command_for(
                    harness, harness, self.workspace, self.workspace / "job",
                    "workspace-write", None, 5, None)
                self.assertNotIn("--model", command)
                self.assertNotIn("--effort", command)
                self.assertNotIn('model_reasoning_effort=', " ".join(command))

    def test_agy_rejects_unsupported_reasoning_effort(self):
        with self.assertRaisesRegex(ValueError, "agy"):
            runner.command_for("agy", "agy", self.workspace, self.workspace / "job",
                               "workspace-write", None, 5, "xhigh")

    def test_claude_json_error_is_not_success(self):
        self.prompt.write_text("JSON_ERROR")
        result = self.run_job("claude")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.state()[0]["status"], "failed")

    def test_claude_success(self):
        result = self.run_job("claude")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.state()[0]["status"], "completed")

    def test_agy_success_requires_explicit_access(self):
        result = subprocess.run(self.command("agy") + ["--access", "workspace-write"],
                                env=self.env, capture_output=True, text=True, timeout=8)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.state()[0]["status"], "completed")

    def test_agy_timeout_is_unknown(self):
        self.prompt.write_text("HANG")
        result = subprocess.run(self.command("agy", "0.3") + ["--access", "workspace-write"],
                                env=self.env, capture_output=True, text=True, timeout=8)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.state()[0]["status"], "unknown")

    def test_agy_cooperative_timeout_is_confirmed(self):
        # 実プロセス・実SIGINT・実時間待機ではなく、run_cooperative_agy()による
        # in-process決定的モックでcooperative timeoutの停止確認を検証する
        # （検証内容はsubprocess版から変更なし。理由はrun_cooperative_agyのdocstring参照）。
        state = self.run_cooperative_agy(wrong_id=False)
        self.assertEqual(state["status"], "timed_out")
        self.assertTrue(state["stop_confirmed"])
        self.assertEqual(state["conversation_id"], "fake-agy")
        self.assertEqual(state["harness_status"], "INTERRUPTED")

    def test_agy_wrong_conversation_cannot_confirm_stop(self):
        # 同上。会話ID不一致時にstop_confirmできずunknownになることを検証する
        # （検証内容はsubprocess版から変更なし）。
        state = self.run_cooperative_agy(wrong_id=True)
        self.assertEqual(state["status"], "unknown")

    def test_agy_structured_error_is_not_success(self):
        self.prompt.write_text("JSON_ERROR")
        result = subprocess.run(self.command("agy") + ["--access", "workspace-write"],
                                env=self.env, capture_output=True, text=True, timeout=8)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.state()[0]["status"], "failed")

    def test_agy_denied_action_is_not_success(self):
        self.prompt.write_text("DENIED")
        result = subprocess.run(self.command("agy") + ["--access", "workspace-write"],
                                env=self.env, capture_output=True, text=True, timeout=8)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.state()[0]["status"], "failed")

    def test_parent_blocked_rejects_launch(self):
        task = self.workspace / ".orchestration/task-1"
        task.mkdir(parents=True)
        (task / "state.json").write_text(json.dumps({"phase": "blocked"}))
        self.assertNotEqual(self.run_job().returncode, 0)
        self.assertEqual(list((task / "jobs").iterdir()), [])

    def test_cli_failure_is_reported(self):
        self.prompt.write_text("FAIL")
        self.assertNotEqual(self.run_job().returncode, 0)
        state, _ = self.state()
        self.assertEqual(state["exit_code"], 7)
        self.assertEqual(state["status"], "failed")

    def test_empty_final_response_is_not_success(self):
        self.prompt.write_text("EMPTY")
        self.assertNotEqual(self.run_job().returncode, 0)
        self.assertEqual(self.state()[0]["status"], "failed")

    def test_timeout(self):
        self.prompt.write_text("HANG")
        self.assertNotEqual(self.run_job(timeout="0.3").returncode, 0)
        self.assertEqual(self.state()[0]["status"], "timed_out")

    def test_duplicate_rejected_and_cancel_acknowledged(self):
        self.prompt.write_text("HANG")
        process = subprocess.Popen(self.command(), env=self.env, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True)
        self.addCleanup(lambda: process.poll() is None and process.kill())
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            paths = list((self.workspace / ".orchestration/task-1/jobs").glob("*/state.json"))
            if paths and json.loads(paths[0].read_text())["status"] == "running":
                break
            time.sleep(0.03)
        else:
            self.fail("runner did not start")
        self.assertNotEqual(self.run_job().returncode, 0)
        job = paths[0].parent
        cancel = subprocess.run([sys.executable, str(RUNNER), "cancel", "--job", str(job)],
                                env=self.env, capture_output=True, text=True, timeout=4)
        self.assertEqual(cancel.returncode, 0, cancel.stderr)
        process.communicate(timeout=5)
        self.assertEqual(self.state()[0]["status"], "cancelled")

    def test_unresolved_old_job_blocks_restart(self):
        self.run_job()
        state, job = self.state()
        state["status"] = "running"
        (job / "state.json").write_text(json.dumps(state))
        self.assertNotEqual(self.run_job().returncode, 0)
        self.assertEqual(len(list(job.parent.iterdir())), 1)

    def direct_args(self):
        return type("Args", (), dict(workspace=self.workspace, task_id="task-1", harness="codex",
                                    role="reviewer", prompt_file=self.prompt, timeout=5,
                                    access="read-only", model=None, reasoning_effort=None))()

    def test_interrupt_during_cleanup_is_not_success(self):
        original = runner.stop_group

        def interrupted_cleanup(process):
            original(process)
            os.kill(os.getpid(), signal.SIGTERM)

        with mock.patch.dict(os.environ, self.env), mock.patch.object(runner, "stop_group", interrupted_cleanup):
            self.assertNotEqual(runner.run(self.direct_args()), 0)
        self.assertEqual(self.state()[0]["status"], "cancelled")

    def test_state_write_failure_still_cleans_exited_child_group(self):
        original = runner.write_json

        def fail_running(path, value):
            if value["status"] == "running":
                raise OSError("simulated state write failure")
            original(path, value)

        process = mock.Mock(pid=999999, returncode=0)
        process.poll.return_value = 0
        with mock.patch.dict(os.environ, self.env), mock.patch.object(runner, "write_json", fail_running), \
                mock.patch.object(runner.subprocess, "Popen", return_value=process), \
                mock.patch.object(runner, "stop_group") as stop:
            self.assertNotEqual(runner.run(self.direct_args()), 0)
            stop.assert_called_once_with(process)


if __name__ == "__main__":
    unittest.main()
