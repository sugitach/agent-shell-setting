"""orchestrator の役割別既定値解決を検証する。"""

import importlib.util
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
RESOLVER = ROOT / "skills/orchestrator/scripts/resolve_role_settings.py"


def load_resolver():
    spec = importlib.util.spec_from_file_location("role_settings", RESOLVER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


SHARED = """version: 1
roles:
  planner:
    model: null
    reasoning_effort: high
  coder:
    model: gpt-shared
    reasoning_effort: medium
  reviewer:
    model: review-model
    reasoning_effort: low
"""


class RoleSettingsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.shared = self.root / "orchestrator-defaults.yaml"
        self.shared.write_text(SHARED)
        self.resolver = load_resolver()

    def resolve(self, role="planner", model=None, effort=None):
        model_arg = self.resolver.UNSET if model is None else model
        effort_arg = self.resolver.UNSET if effort is None else effort
        with mock.patch.object(self.resolver, "SHARED_DEFAULTS", self.shared):
            return self.resolver.resolve_settings(
                self.workspace, role, model_arg, effort_arg)

    def write_project(self, content):
        (self.workspace / "orchestrator-defaults.yaml").write_text(content)

    def test_shared_defaults_include_null_model_and_sources(self):
        result = self.resolve()
        self.assertEqual(result["role"], "planner")
        self.assertEqual(result["settings"], {"model": None, "reasoning_effort": "high"})
        self.assertEqual(result["sources"], {"model": "shared", "reasoning_effort": "shared"})
        self.assertEqual(result["files"], {"shared": str(self.shared), "project": None})

    def test_project_overrides_a_single_field(self):
        self.write_project("""version: 1
roles:
  planner:
    model: project-model
""")
        result = self.resolve()
        self.assertEqual(result["settings"], {"model": "project-model", "reasoning_effort": "high"})
        self.assertEqual(result["sources"], {"model": "project", "reasoning_effort": "shared"})
        self.assertEqual(result["files"]["project"], str(self.workspace.resolve() / "orchestrator-defaults.yaml"))

    def test_omitted_and_explicit_default_are_distinct(self):
        self.write_project("""version: 1
roles:
  planner:
    model: project-model
    reasoning_effort: low
""")
        omitted = self.resolve()
        defaulted = self.resolve(model="default", effort="default")
        self.assertEqual(omitted["settings"], {"model": "project-model", "reasoning_effort": "low"})
        self.assertEqual(defaulted["settings"], {"model": None, "reasoning_effort": None})
        self.assertEqual(defaulted["sources"], {"model": "explicit", "reasoning_effort": "explicit"})

    def test_explicit_values_win_and_json_shape_is_fixed(self):
        self.write_project("""version: 1
roles:
  coder:
    model: project-model
    reasoning_effort: low
""")
        result = self.resolve("coder", "gpt-explicit", "xhigh")
        self.assertEqual(list(result), ["role", "settings", "sources", "files"])
        self.assertEqual(result["settings"], {"model": "gpt-explicit", "reasoning_effort": "xhigh"})
        self.assertEqual(result["sources"], {"model": "explicit", "reasoning_effort": "explicit"})
        self.assertEqual(json.loads(json.dumps(result))["role"], "coder")

    def test_cli_prints_only_one_fixed_json_object(self):
        output = io.StringIO()
        errors = io.StringIO()
        with mock.patch.object(self.resolver, "SHARED_DEFAULTS", self.shared), \
             redirect_stdout(output), redirect_stderr(errors):
            code = self.resolver.main([
                "--workspace", str(self.workspace), "--role", "planner",
                "--model", "default", "--reasoning-effort", "high",
            ])
        self.assertEqual(code, 0)
        self.assertEqual(errors.getvalue(), "")
        self.assertEqual(
            json.loads(output.getvalue()),
            {
                "role": "planner",
                "settings": {"model": None, "reasoning_effort": "high"},
                "sources": {"model": "explicit", "reasoning_effort": "explicit"},
                "files": {"shared": str(self.shared), "project": None},
            },
        )

    def test_rejects_limited_yaml_violations(self):
        cases = {
            "quoted": SHARED.replace("model: null", 'model: "bad"', 1),
            "unicode": SHARED.replace("model: null", "model: あ", 1),
            "comment": SHARED.replace("model: null", "model: null # no", 1),
            "colon": SHARED.replace("model: null", "model: x:y", 1),
            "tab": SHARED.replace("  planner:", "\tplanner:", 1),
            "cr": SHARED.replace("\n", "\r\n"),
            "trailing": SHARED.replace("version: 1", "version: 1 ", 1),
            "blank": SHARED.replace("roles:\n", "roles:\n\n", 1),
            "marker": "---\n" + SHARED,
            "unknown": SHARED.replace("roles:\n", "roles:\n  unknown:\n    model: x\n", 1),
            "duplicate": SHARED.replace("    model: null\n", "    model: null\n    model: x\n", 1),
            "order": SHARED.replace("    model: null\n    reasoning_effort: high", "    reasoning_effort: high\n    model: null", 1),
            "missing-field": SHARED.replace("    reasoning_effort: high\n", "", 1),
            "yaml-default": SHARED.replace("model: null", "model: default", 1),
        }
        for name, content in cases.items():
            with self.subTest(name=name):
                self.shared.write_text(content)
                with self.assertRaises(ValueError):
                    self.resolve()
                self.shared.write_text(SHARED)

    def test_project_requires_version_roles_and_a_field(self):
        cases = [
            "roles:\n  planner:\n    model: x\n",
            "version: 1\n  planner:\n    model: x\n",
            "version: 1\nroles:\n",
        ]
        for content in cases:
            with self.subTest(content=content):
                self.write_project(content)
                with self.assertRaises(ValueError):
                    self.resolve()
                (self.workspace / "orchestrator-defaults.yaml").unlink()

    def test_rejects_oversize_files_before_parsing(self):
        oversized = b"x" * (64 * 1024 + 1)
        self.shared.write_bytes(oversized)
        with self.assertRaisesRegex(ValueError, "64 KiB"):
            self.resolve()
        self.shared.write_text(SHARED)
        (self.workspace / "orchestrator-defaults.yaml").write_bytes(oversized)
        with self.assertRaisesRegex(ValueError, "64 KiB"):
            self.resolve()

    def test_rejects_bad_workspace_and_non_regular_candidates(self):
        with self.assertRaises(ValueError):
            self.resolver.resolve_settings(Path("relative"), "planner", self.resolver.UNSET, self.resolver.UNSET)
        with self.assertRaises(ValueError):
            self.resolver.resolve_settings(self.root / "missing", "planner", self.resolver.UNSET, self.resolver.UNSET)
        file_workspace = self.root / "file"
        file_workspace.write_text("x")
        with self.assertRaises(ValueError):
            self.resolver.resolve_settings(file_workspace, "planner", self.resolver.UNSET, self.resolver.UNSET)
        with mock.patch.object(self.resolver, "SHARED_DEFAULTS", self.shared):
            project = self.workspace / "orchestrator-defaults.yaml"
            project.mkdir()
            with self.assertRaises(ValueError):
                self.resolver.resolve_settings(self.workspace, "planner", self.resolver.UNSET, self.resolver.UNSET)
            project.rmdir()
            project.symlink_to(self.shared)
            with self.assertRaises(ValueError):
                self.resolver.resolve_settings(self.workspace, "planner", self.resolver.UNSET, self.resolver.UNSET)

    def test_rejects_shared_symlink_and_fifo(self):
        target = self.root / "target.yaml"
        target.write_text(SHARED)
        self.shared.unlink()
        self.shared.symlink_to(target)
        with self.assertRaises(ValueError):
            self.resolve()
        self.shared.unlink()
        os.mkfifo(self.shared)
        with self.assertRaises(ValueError):
            self.resolve()


if __name__ == "__main__":
    unittest.main()
