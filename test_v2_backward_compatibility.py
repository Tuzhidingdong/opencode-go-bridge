import os
import sys
import json
import time
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import opencode_go_worker_core as core
import opencode_go_worker_mcp as mcp


class LegacyClientWithoutManifest(core.OpenCodeClient):
    """Simulates a legacy OpenCodeClient whose start_worker() DOES NOT have benchmark_manifest."""

    def __init__(self, db_path: str):
        super().__init__()
        self.store.path = Path(db_path)
        self.store._init_db()
        self.calls = []

    def ensure_server(self, raw_project_root=None):
        return None

    def models_and_usage(self, raw_root, **kw):
        return {
            "agents": [{"id": "build"}, {"id": "explore"}],
            "models": [{"id": "qwen3.8-flash"}],
            "required_agents_available": {"build": True, "explore": True},
        }

    def request(self, method, path, body=None, **kw):
        return {"data": {"id": "ses_mock_legacy"}}

    # Explicitly define start_worker WITHOUT benchmark_manifest (legacy V1 signature)
    def start_worker(
        self,
        raw_root: str,
        role: str,
        task: str,
        model_id: str,
        title: str | None = None,
        allow_over_limit: bool = False,
        worker_slot: str | None = None,
        session_strategy: str = "new",
        session_id: str | None = None,
        decision_reason: str | None = None,
        estimated_cost: dict | None = None,
        prediction_confidence: float | None = None,
    ) -> dict:
        self.calls.append({
            "raw_root": raw_root,
            "role": role,
            "task": task,
            "model_id": model_id,
            "session_strategy": session_strategy,
        })
        # Call underlying logic or return simulated result
        job_id = f"job_legacy_{int(time.time()*1000)}"
        return {
            "job_id": job_id,
            "session_id": "ses_mock_legacy",
            "worker_slot": worker_slot or role,
            "session_strategy": session_strategy,
            "role": role,
            "model": f"opencode-go/{model_id}",
            "project_root": core.canonical_project_root(raw_root),
            "state": "submitted",
        }


class V2BackwardCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.project_root = os.path.join(self.temp_dir, "test_proj")
        os.makedirs(self.project_root, exist_ok=True)

        self.db_path = os.path.join(self.temp_dir, "test_jobs.sqlite3")
        self.prev_db = os.environ.get("OPENCODE_GO_WORKER_DB")
        os.environ["OPENCODE_GO_WORKER_DB"] = self.db_path

        self.client = core.OpenCodeClient()
        self.client.store.path = Path(self.db_path)
        self.client.store._init_db()

        # Mock server & network requests to avoid real execution
        self.client.ensure_server = lambda root=None: None
        self.client.models_and_usage = lambda r, **kw: {
            "agents": [{"id": "build"}, {"id": "explore"}],
            "models": [{"id": "qwen3.8-flash"}],
            "required_agents_available": {"build": True, "explore": True},
        }
        self._req_counter = 0
        def _mock_req(m, p, b=None, **kw):
            self._req_counter += 1
            return {"data": {"id": f"ses_mock_v2_{self._req_counter}_{int(time.time()*1000)}"}}
        self.client.request = _mock_req

    def tearDown(self):
        if self.prev_db:
            os.environ["OPENCODE_GO_WORKER_DB"] = self.prev_db
        else:
            os.environ.pop("OPENCODE_GO_WORKER_DB", None)
        try:
            shutil.rmtree(self.temp_dir, ignore_errors=True)
        except Exception:
            pass

    def test_scenario_01_legacy_mcp_call_without_manifest(self):
        """Scenario 1: Original opencode_go_start call via MCP without benchmark_manifest works normally."""
        with patch.object(mcp, "CLIENT", self.client):
            args = {
                "project_root": self.project_root,
                "role": "reviewer",
                "task": "Review legacy code without manifest",
                "model_id": "opencode-go/qwen3.8-flash",
                "session_strategy": "new",
            }
            res = mcp.handle_tool("opencode_go_start", args)
            self.assertFalse(res.get("isError", False))
            structured = res["structuredContent"]
            self.assertEqual(structured["role"], "reviewer")
            self.assertEqual(structured["state"], "submitted")
            self.assertTrue(structured["job_id"].startswith("job_"))

    def test_scenario_02_legacy_client_call_without_manifest(self):
        """Scenario 2: Original Client.start_worker call without benchmark_manifest works normally."""
        res = self.client.start_worker(
            raw_root=self.project_root,
            role="writer",
            task="Write feature directly using client",
            model_id="qwen3.8-flash",
        )
        self.assertEqual(res["role"], "writer")
        self.assertEqual(res["state"], "submitted")
        self.assertTrue(res["job_id"].startswith("job_"))

    def test_scenario_03_positional_arguments_compatibility(self):
        """Scenario 3: Calling Client.start_worker with positional arguments preserves exact order."""
        # 4 positional arguments
        res1 = self.client.start_worker(
            self.project_root,
            "analyst",
            "Positional task 4 args",
            "qwen3.8-flash",
        )
        self.assertEqual(res1["role"], "analyst")

        # 8 positional arguments
        res2 = self.client.start_worker(
            self.project_root,
            "analyst",
            "Positional task 8 args",
            "qwen3.8-flash",
            "Custom Title",
            False,
            "analyst_slot",
            "new",
        )
        self.assertEqual(res2["worker_slot"], "analyst_slot")

    def test_scenario_04_v2_mcp_call_with_manifest_persists_metadata(self):
        """Scenario 4: V2 call with benchmark_manifest correctly records manifest in JobStore."""
        manifest_meta = {
            "benchmark_name": "Phase_D_Replay",
            "benchmark_phase": "Phase_D_Review",
            "prompt_file": "test_prompt.txt",
            "input_file": "test01_frozen.txt",
            "parameters": {"temperature": 0.2},
        }
        with patch.object(mcp, "CLIENT", self.client):
            args = {
                "project_root": self.project_root,
                "role": "reviewer",
                "task": "Review with manifest metadata",
                "model_id": "opencode-go/qwen3.8-flash",
                "session_strategy": "new",
                "benchmark_manifest": manifest_meta,
            }
            res = mcp.handle_tool("opencode_go_start", args)
            self.assertFalse(res.get("isError", False))
            structured = res["structuredContent"]
            job_id = structured["job_id"]

            # Query manifest to verify persistence
            mf = self.client.get_benchmark_manifest(job_id)
            self.assertIsNotNone(mf)
            self.assertEqual(mf["benchmark_name"], "Phase_D_Replay")
            self.assertEqual(mf["input_file"], "test01_frozen.txt")
            self.assertEqual(mf["job_id"], job_id)

    def test_scenario_05_v2_mcp_call_with_none_manifest(self):
        """Scenario 5: V2 call with benchmark_manifest=None works without errors."""
        with patch.object(mcp, "CLIENT", self.client):
            args = {
                "project_root": self.project_root,
                "role": "reviser",
                "task": "Reviser task with explicit None manifest",
                "model_id": "opencode-go/qwen3.8-flash",
                "benchmark_manifest": None,
            }
            res = mcp.handle_tool("opencode_go_start", args)
            self.assertFalse(res.get("isError", False))
            self.assertEqual(res["structuredContent"]["role"], "reviser")

    def test_scenario_06_decoupled_mcp_with_legacy_client_instance(self):
        """Scenario 6: MCP dispatch handles legacy Client whose start_worker() does NOT accept benchmark_manifest."""
        legacy_client = LegacyClientWithoutManifest(self.db_path)
        with patch.object(mcp, "CLIENT", legacy_client):
            manifest_meta = {
                "benchmark_name": "Legacy_Client_Test",
                "benchmark_phase": "Phase_1",
            }
            args = {
                "project_root": self.project_root,
                "role": "reviewer",
                "task": "Calling MCP backed by legacy client",
                "model_id": "opencode-go/qwen3.8-flash",
                "benchmark_manifest": manifest_meta,
            }
            # This MUST NOT raise TypeError!
            res = mcp.handle_tool("opencode_go_start", args)
            self.assertFalse(res.get("isError", False))
            self.assertEqual(res["structuredContent"]["role"], "reviewer")
            # Verify legacy client was called cleanly
            self.assertEqual(len(legacy_client.calls), 1)

    def test_scenario_07_invalid_manifest_error_handling(self):
        """Scenario 7: Invalid manifest returns structured APPLICATION_ERROR and does not crash MCP."""
        # 1. MCP level invalid manifest (string instead of dict)
        with patch.object(mcp, "CLIENT", self.client):
            args = {
                "project_root": self.project_root,
                "role": "reviewer",
                "task": "Review with invalid manifest",
                "model_id": "opencode-go/qwen3.8-flash",
                "benchmark_manifest": "invalid_string_not_dict",
            }
            res = mcp.handle_tool("opencode_go_start", args)
            self.assertTrue(res.get("isError", False))
            self.assertIn("benchmark_manifest 必须是字典对象", res["content"][0]["text"])

        # 2. Client level invalid manifest
        with self.assertRaises(core.BridgeError) as cm:
            self.client.start_worker(
                raw_root=self.project_root,
                role="reviewer",
                task="Task with invalid manifest",
                model_id="qwen3.8-flash",
                benchmark_manifest=["invalid", "list"],
            )
        self.assertEqual(cm.exception.category, "APPLICATION_ERROR")
        self.assertIn("benchmark_manifest 必须是字典对象", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
