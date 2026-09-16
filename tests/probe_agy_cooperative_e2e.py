"""実機に近い形（実プロセス・実SIGINT）でCOOPERATIVE分岐の協調停止を検証する参考テスト。
flaky を構造的にゼロにはできないため、十分な timeout/grace マージン（十分な余裕を持った
--timeout 2 / --cancel-grace 5）を取った上で、CI必須チェック対象外の参考ジョブとしてのみ
実行する。失敗した場合はタイミング運ではなく実装上の回帰を疑うこと
（tests/test_external_runner.py の test_agy_cooperative_timeout_is_confirmed が担う
決定的な検証とは役割が異なる。DoDはそちらのin-process決定的テストが担う）。

ファイル名を probe_*.py とし、test_*.py パターンに一致させないことで、
`python -m unittest discover -s tests -p "test_*.py"`（CIの test ジョブ・DoD検証コマンドの
双方が使うパターン）には一切影響を与えない（既存の tests/probe_agy_cancel.py と同じ命名慣行）。
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "skills/orchestrator/scripts/external_runner.py"


class AgyCooperativeE2EProbe(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name)
        bindir = self.workspace / "bin"
        bindir.mkdir()
        dest = bindir / "agy"
        shutil.copy2(ROOT / "tests/fixtures/fake_harness.py", dest)
        dest.chmod(0o700)
        self.env = dict(os.environ, PATH=str(bindir) + os.pathsep + os.environ["PATH"])
        self.prompt = self.workspace / "prompt.md"
        self.prompt.write_text("COOPERATIVE")

    def test_real_process_cooperative_sigint_is_confirmed(self):
        command = [sys.executable, str(RUNNER), "run", "--workspace", str(self.workspace),
                   "--task-id", "task-1", "--harness", "agy", "--role", "reviewer",
                   "--prompt-file", str(self.prompt), "--access", "workspace-write",
                   "--agy-project", "probe-project", "--timeout", "2", "--cancel-grace", "5"]
        result = subprocess.run(command, env=self.env, capture_output=True, text=True, timeout=20)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        states = list((self.workspace / ".orchestration/task-1/jobs").glob("*/state.json"))
        self.assertEqual(len(states), 1)
        state = json.loads(states[0].read_text())
        self.assertEqual(state["status"], "timed_out")
        self.assertTrue(state["stop_confirmed"])
        self.assertEqual(state["conversation_id"], "fake-agy")
        self.assertEqual(state["harness_status"], "INTERRUPTED")


if __name__ == "__main__":
    unittest.main()
