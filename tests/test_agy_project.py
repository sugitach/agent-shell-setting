"""Agy Project ID の安全な解決を検証する。"""

import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
RESOLVER = ROOT / "skills/orchestrator/scripts/resolve_agy_project.py"
PROFILES = ROOT / "skills/orchestrator/agy-permission-profiles.yaml"


def load_resolver():
    spec = importlib.util.spec_from_file_location("agy_project", RESOLVER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class AgyProjectTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name) / "workspace"
        self.workspace.mkdir()
        self.resolver = load_resolver()

    def config(self, project_id):
        path = self.workspace / ".orchestration"
        path.mkdir(exist_ok=True)
        (path / "agy-project.json").write_text(json.dumps({"project_id": project_id}))

    def test_explicit_project_has_highest_priority(self):
        self.config("file-project")
        with mock.patch.dict(os.environ, {"AGY_PROJECT_ID": "environment-project"}):
            self.assertEqual(
                self.resolver.resolve_agy_project(self.workspace, "explicit-project"),
                "explicit-project")

    def test_environment_project_precedes_workspace_file(self):
        self.config("file-project")
        with mock.patch.dict(os.environ, {"AGY_PROJECT_ID": "environment-project"}):
            self.assertEqual(
                self.resolver.resolve_agy_project(self.workspace), "environment-project")

    def test_workspace_file_is_used_when_no_higher_source_exists(self):
        self.config("file-project")
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(self.resolver.resolve_agy_project(self.workspace), "file-project")

    def test_unresolved_project_fails_without_default_fallback(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, "agy project id could not be resolved"):
                self.resolver.resolve_agy_project(self.workspace)

    def test_rejects_invalid_workspace_file(self):
        path = self.workspace / ".orchestration"
        path.mkdir()
        (path / "agy-project.json").write_text('{"project_id":"ok", "extra":true}')
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ValueError):
                self.resolver.resolve_agy_project(self.workspace)

    def test_rejects_symlink_and_oversized_workspace_file(self):
        path = self.workspace / ".orchestration"
        path.mkdir()
        target = self.workspace / "target.json"
        target.write_text('{"project_id":"ok"}')
        config = path / "agy-project.json"
        config.symlink_to(target)
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ValueError):
                self.resolver.resolve_agy_project(self.workspace)
        config.unlink()
        config.write_bytes(b"x" * (4096 + 1))
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, "4 KiB"):
                self.resolver.resolve_agy_project(self.workspace)

    def test_permission_profiles_declare_the_role_boundaries(self):
        self.assertEqual(PROFILES.read_text(), """version: 1
roles:
  planner:
    read:
      - \".\"
    write:
      - \".orchestration\"
  coder:
    read:
      - \".\"
    write:
      - \".\"
  reviewer:
    read:
      - \".\"
    write:
      - \".orchestration\"
""")


if __name__ == "__main__":
    unittest.main()
