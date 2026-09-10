"""リンクからコピーへの移行と再実行時の動作を検証する。"""

import importlib.util
import tempfile
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location("deploy", Path(__file__).resolve().parents[1] / "scripts/deploy.py")
deploy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deploy)


class DeployTest(unittest.TestCase):
    def test_migration_and_idempotence(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            original = home / "old-rules.md"
            original.write_text("original")
            target = home / ".codex/AGENTS.md"
            target.parent.mkdir()
            target.symlink_to(original)
            unrelated = home / ".claude/skills/unrelated/SKILL.md"
            unrelated.parent.mkdir(parents=True)
            unrelated.write_text("keep")
            self.assertEqual(deploy.deploy(home), 1)
            self.assertTrue(target.is_symlink())
            deploy.deploy(home, apply=True)
            self.assertFalse(target.is_symlink())
            self.assertEqual(original.read_text(), "original")
            self.assertEqual(unrelated.read_text(), "keep")
            backups = list((home / ".agent-shell-setting-backups").iterdir())
            self.assertEqual(len(backups), 1)
            self.assertTrue((backups[0] / ".codex/AGENTS.md").is_symlink())
            self.assertEqual(deploy.deploy(home), 0)
            deploy.deploy(home, apply=True)
            self.assertEqual(list((home / ".agent-shell-setting-backups").iterdir()), backups)

    def test_symlink_parent_rejected_before_writes(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder) / "home"
            home.mkdir()
            other = Path(folder) / "other"
            other.mkdir()
            (home / ".claude").symlink_to(other, target_is_directory=True)
            with self.assertRaises(ValueError):
                deploy.deploy(home, apply=True)
            self.assertFalse((home / ".gemini").exists())


if __name__ == "__main__":
    unittest.main()
