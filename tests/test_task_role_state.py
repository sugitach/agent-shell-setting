"""固定化される orchestrator の役割 state を検証する。"""

import importlib.util
import json
import io
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills/orchestrator/scripts"
sys.path.insert(0, str(SCRIPTS))
MODULE = SCRIPTS / "task_role_state.py"


def load_module():
    spec = importlib.util.spec_from_file_location("task_role_state", MODULE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TaskRoleStateTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name) / "workspace"
        self.workspace.mkdir()
        self.module = load_module()
        self.files = {"shared": "/skills/defaults.yaml", "project": None}
        self.calls = []

    def resolver(self, workspace, role, model, effort, *, harness):
        self.calls.append((role, harness, model, effort))
        return {
            "role": role,
            "settings": {"harness": harness if harness is not self.module.UNSET else "codex",
                         "model": model if model is not self.module.UNSET else None,
                         "reasoning_effort": effort if effort is not self.module.UNSET else "high"},
            "sources": {"harness": "explicit", "model": "shared", "reasoning_effort": "shared"},
            "files": self.files,
        }

    def metadata(self):
        return {"task_id": "issue-3", "owner_session": "parent", "phase": "planning",
                "active_child": None}

    def test_init_resolves_all_roles_once_and_preserves_metadata(self):
        state = self.metadata()
        result = self.module.initialize_roles(state, self.workspace, {"planner": {"harness": "claude"}}, self.resolver)
        self.assertEqual(state, self.metadata())
        self.assertEqual([call[0] for call in self.calls], ["planner", "coder", "reviewer"])
        self.assertEqual(result["task_id"], "issue-3")
        self.assertEqual(result["roles"]["planner"]["harness"], "claude")
        self.assertEqual(set(result["roles"]["coder"]), {"harness", "model", "reasoning_effort", "sources"})
        self.assertEqual(result["role_settings_files"], self.files)

    def test_existing_roles_and_continue_do_not_resolve_again(self):
        snapshot = self.module.initialize_roles(self.metadata(), self.workspace, {}, self.resolver)
        self.calls.clear()
        initialized = self.module.initialize_roles(snapshot, self.workspace, {}, self.resolver)
        roles, migrated = self.module.roles_for_continuation(initialized)
        self.assertEqual(self.calls, [])
        self.assertFalse(migrated)
        self.assertEqual(roles["planner"], snapshot["roles"]["planner"])

    def test_resume_replaces_all_roles_and_records_history(self):
        state = self.module.initialize_roles(self.metadata(), self.workspace, {}, self.resolver)
        self.calls.clear()
        resumed = self.module.resume_roles(state, self.workspace, {"reviewer": {"harness": "claude"}}, "user request", self.resolver)
        self.assertEqual([call[0] for call in self.calls], ["planner", "coder", "reviewer"])
        self.assertEqual(resumed["roles"]["reviewer"]["harness"], "claude")
        history = resumed["role_settings_history"][-1]
        self.assertEqual(history["reason"], "user request")
        self.assertIn("previous_roles", history)
        self.assertIn("resumed_files", history)

    def test_resume_rejects_active_child_before_resolving(self):
        state = self.module.initialize_roles(self.metadata(), self.workspace, {}, self.resolver)
        state["active_child"] = {"role": "planner"}
        self.calls.clear()
        with self.assertRaises(ValueError):
            self.module.resume_roles(state, self.workspace, {}, "user request", self.resolver)
        self.assertEqual(self.calls, [])

    def test_legacy_forms_normalize_without_reader(self):
        state = self.module.initialize_roles(self.metadata(), self.workspace, {}, self.resolver)
        for record in state["roles"].values():
            del record["sources"]["harness"]
        roles, migrated = self.module.roles_for_continuation(state)
        self.assertTrue(migrated)
        self.assertTrue(all(record["sources"]["harness"] == "legacy" for record in roles.values()))
        legacy = self.metadata()
        legacy["roles"] = {role: record["harness"] for role, record in state["roles"].items()}
        legacy["role_settings_files"] = self.files
        legacy["role_settings"] = {role: {key: record[key] for key in ("model", "reasoning_effort", "sources")}
                                   for role, record in state["roles"].items()}
        roles, migrated = self.module.roles_for_continuation(legacy)
        self.assertTrue(migrated)
        self.assertTrue(all(record["sources"]["harness"] == "legacy" for record in roles.values()))

    def test_invalid_state_and_atomic_writer_are_rejected_safely(self):
        with self.assertRaises(ValueError):
            self.module.initialize_roles({"role_settings": {}}, self.workspace, {}, self.resolver)
        with self.assertRaises(ValueError):
            self.module.initialize_roles(self.metadata(), self.workspace, {"other": {}}, self.resolver)
        state_path = self.workspace / "state.json"
        self.module.atomic_write_state(state_path, self.metadata())
        self.assertEqual(json.loads(state_path.read_text())["task_id"], "issue-3")
        state_path.unlink()
        state_path.symlink_to(self.workspace / "target")
        with self.assertRaises(ValueError):
            self.module.atomic_write_state(state_path, self.metadata())

    def test_atomic_writer_completes_partial_writes_and_cleans_up_failures(self):
        state_path = self.workspace / "state.json"
        previous = {"task_id": "previous"}
        self.module.atomic_write_state(state_path, previous)
        replacement = {"task_id": "replacement", "large": "x" * 4096}
        original_write = self.module.os.write

        def partial_write(descriptor, data):
            count = min(7, len(data))
            original_write(descriptor, data[:count])
            return count

        with mock.patch.object(self.module.os, "write", side_effect=partial_write):
            self.module.atomic_write_state(state_path, replacement)
        self.assertEqual(json.loads(state_path.read_text()), replacement)

        with mock.patch.object(self.module.os, "write", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.module.atomic_write_state(state_path, {"task_id": "broken"})
        self.assertEqual(json.loads(state_path.read_text()), replacement)
        self.assertFalse((self.workspace / "state.json.tmp").exists())

    def test_cli_init_continue_and_resume(self):
        state_path = self.workspace / "state.json"
        overrides_path = self.workspace / "overrides.json"
        state_path.write_text(json.dumps(self.metadata()))
        overrides_path.write_text(json.dumps({"planner": {"harness": "codex"}}))
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(io.StringIO()):
            self.assertEqual(self.module.main([
                "init", "--workspace", str(self.workspace), "--state", str(state_path),
                "--overrides-file", str(overrides_path),
            ]), 0)
            self.assertEqual(self.module.main([
                "continue", "--state", str(state_path), "--role", "planner",
            ]), 0)
            self.assertEqual(self.module.main([
                "resume", "--workspace", str(self.workspace), "--state", str(state_path),
                "--overrides-file", str(overrides_path), "--reason", "user request",
            ]), 0)
        state = json.loads(state_path.read_text())
        self.assertEqual(state["roles"]["planner"]["harness"], "codex")
        self.assertEqual(len(state["role_settings_history"]), 1)


if __name__ == "__main__":
    unittest.main()
