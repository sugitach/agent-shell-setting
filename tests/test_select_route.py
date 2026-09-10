"""役割ハーネスを維持した方式選択を検証する。"""

import importlib.util
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "select_route", Path(__file__).resolve().parents[1]
    / "skills/orchestrator/scripts/select_route.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class RouteTest(unittest.TestCase):
    def test_native_preferred_for_same_harness(self):
        for harness in ("claude", "codex", "agy"):
            self.assertEqual(module.select_route(harness, harness, True, True)["transport"], "native")

    def test_cross_harness_never_native(self):
        result = module.select_route("claude", "codex", True, True)
        self.assertEqual(result["transport"], "external")
        self.assertEqual(result["harness"], "codex")

    def test_unavailable_native_falls_back(self):
        self.assertEqual(module.select_route("codex", "codex", False, True)["transport"], "external")

    def test_isolation_requires_external(self):
        self.assertEqual(module.select_route("codex", "codex", True, True, True)["transport"], "external")

    def test_explicit_mode_is_not_overridden(self):
        self.assertEqual(module.select_route("codex", "codex", True, True, mode="external")["transport"], "external")
        self.assertEqual(module.select_route("claude", "codex", True, True, mode="native")["status"], "blocked")
        self.assertEqual(module.select_route("codex", "codex", True, False, mode="external")["status"], "blocked")

    def test_unverified_capability_blocks(self):
        self.assertEqual(module.select_route("codex", "codex")["status"], "blocked")


if __name__ == "__main__":
    unittest.main()
