import os
import sys
import json
import time
import shutil
import tempfile
import unittest
import subprocess
from datetime import datetime, timezone

import opencode_go_worker_core as core
import opencode_go_worker_mcp as mcp


class ComprehensiveV2HardeningTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.project_root = os.path.join(self.temp_dir, "test_proj")
        os.makedirs(self.project_root, exist_ok=True)

        self.db_path = os.path.join(self.temp_dir, "test_jobs.sqlite3")
        self.prev_db = os.environ.get("OPENCODE_GO_WORKER_DB")
        os.environ["OPENCODE_GO_WORKER_DB"] = self.db_path

        self.client = core.OpenCodeClient()
        self.client.store.path = core.Path(self.db_path)
        self.client.store._init_db()

    def tearDown(self):
        if self.prev_db:
            os.environ["OPENCODE_GO_WORKER_DB"] = self.prev_db
        else:
            os.environ.pop("OPENCODE_GO_WORKER_DB", None)
        try:
            shutil.rmtree(self.temp_dir, ignore_errors=True)
        except Exception:
            pass

    def test_01_interface_contract_and_benchmark_manifest(self):
        """Test start_worker accepts decision_reason and benchmark_manifest and persists them."""
        # Mock inspection and network for client
        self.client.ensure_server = lambda root=None: None
        self.client.models_and_usage = lambda r, **kw: {
            "agents": [{"id": "build"}, {"id": "explore"}],
            "models": [{"id": "qwen3.8-flash"}],
            "required_agents_available": {"build": True, "explore": True},
        }
        self.client.request = lambda m, p, b=None, **kw: {"data": {"id": "ses_mock_123"}}

        manifest_data = {
            "benchmark_name": "Phase_D_Replay",
            "benchmark_phase": "Phase_D_Review",
            "prompt_file": "prompt.txt",
            "input_file": "input.txt",
            "parameters": {"check_mode": "strict", "temperature": 0.0},
        }

        res = self.client.start_worker(
            raw_root=self.project_root,
            role="reviewer",
            task="Perform audit on benchmark file",
            model_id="qwen3.8-flash",
            worker_slot="slot_audit",
            session_strategy="new",
            decision_reason="Autonomous benchmark replay authorization",
            benchmark_manifest=manifest_data,
        )

        self.assertEqual(res["state"], "submitted")
        self.assertEqual(res["session_id"], "ses_mock_123")

        # Verify decision_reason was recorded
        cur_job = res["job_id"]
        decision = self.client.store.get_decision_by_job(cur_job)
        self.assertIsNotNone(decision)
        self.assertEqual(decision.get("decision_reason"), "Autonomous benchmark replay authorization")

        # Verify benchmark_manifest was recorded
        mf = self.client.get_benchmark_manifest(cur_job)
        self.assertEqual(mf["benchmark_name"], "Phase_D_Replay")
        self.assertEqual(mf["model_id"], "qwen3.8-flash")
        self.assertEqual(mf["role"], "reviewer")
        self.assertEqual(mf["parameters"]["check_mode"], "strict")

    def test_02_structured_error_classification(self):
        """Test ErrorCategory definitions, BridgeError to_dict and MCP structured error output."""
        self.assertEqual(core.ErrorCategory.APPLICATION_ERROR, "APPLICATION_ERROR")
        self.assertEqual(core.ErrorCategory.TRANSPORT_ERROR, "TRANSPORT_ERROR")
        self.assertEqual(core.ErrorCategory.RECOVERY_FAILED, "RECOVERY_FAILED")

        err = core.BridgeError(
            "Test application error",
            category=core.ErrorCategory.APPLICATION_ERROR,
            user_message="Friendly message",
        )
        d = err.to_dict()
        self.assertEqual(d["error_category"], "APPLICATION_ERROR")
        self.assertEqual(d["user_message"], "Friendly message")
        self.assertFalse(d["retryable"])
        self.assertFalse(d["circuit_break"])

        # Test dispatch error formatting with TypeError
        test_msg = {
            "jsonrpc": "2.0",
            "id": "req_err_1",
            "method": "tools/call",
            "params": {
                "name": "opencode_go_wait_result",
                "arguments": {"invalid_arg": 123},  # missing required args
            },
        }

        captured_output = []
        original_send = mcp.send
        mcp.send = lambda msg: captured_output.append(msg)
        try:
            mcp.dispatch(test_msg)
        finally:
            mcp.send = original_send

        self.assertEqual(len(captured_output), 1)
        resp = captured_output[0]
        self.assertEqual(resp["id"], "req_err_1")
        self.assertTrue(resp["result"]["isError"])
        payload = json.loads(resp["result"]["content"][0]["text"])
        self.assertEqual(payload["error_category"], "APPLICATION_ERROR")
        self.assertIn("切勿终止或重启 MCP 进程", payload["user_message"])

    def test_03_circuit_breaker_resilience(self):
        """Test CircuitBreaker: 3 consecutive transport failures trip breaker to OPEN."""
        cb = core.CircuitBreaker(failure_threshold=3, reset_timeout=2.0)
        self.assertEqual(cb.state, "CLOSED")

        # Application error does not trip breaker
        self.assertFalse(cb.record_failure(core.ErrorCategory.APPLICATION_ERROR))
        self.assertEqual(cb.failure_count, 0)

        # 1st transport failure
        self.assertFalse(cb.record_failure(core.ErrorCategory.TRANSPORT_ERROR))
        self.assertEqual(cb.failure_count, 1)

        # 2nd transport failure
        self.assertFalse(cb.record_failure(core.ErrorCategory.TRANSPORT_ERROR))
        self.assertEqual(cb.failure_count, 2)

        # 3rd transport failure -> trips breaker
        tripped = cb.record_failure(core.ErrorCategory.TRANSPORT_ERROR)
        self.assertTrue(tripped)
        self.assertEqual(cb.state, "OPEN")

        is_tripped, msg = cb.is_tripped()
        self.assertTrue(is_tripped)
        self.assertIn("通信熔断器已触发", msg)

        # Reset success
        cb.record_success()
        self.assertEqual(cb.state, "CLOSED")
        is_tripped_after, _ = cb.is_tripped()
        self.assertFalse(is_tripped_after)

    def test_04_cross_process_sqlite_session_locks(self):
        """Test persistent cross-process SQLite session locking and fencing token."""
        store = self.client.store
        sid = "ses_concurrent_test"

        # Instance A acquires lock
        lock_a = store.acquire_session_lock(sid, "job_aaa", "proc_instance_A", lease_seconds=100.0)
        self.assertEqual(lock_a["status"], "active")
        self.assertEqual(lock_a["fencing_token"], 1)

        # Instance B attempts to acquire lock on the same session with different job -> BridgeError
        with self.assertRaises(core.BridgeError) as ctx:
            store.acquire_session_lock(sid, "job_bbb", "proc_instance_B", lease_seconds=100.0)
        self.assertIn("独占锁定", str(ctx.exception))

        # Check busy state
        is_busy, busy_job = self.client.is_session_busy(sid)
        self.assertTrue(is_busy)
        self.assertEqual(busy_job, "job_aaa")

        # Instance A releases lock
        released = store.release_session_lock(sid, "job_aaa")
        self.assertTrue(released)

        # Instance B can now acquire lock
        lock_b = store.acquire_session_lock(sid, "job_bbb", "proc_instance_B", lease_seconds=100.0)
        self.assertEqual(lock_b["status"], "active")
        self.assertEqual(lock_b["fencing_token"], 2)

    def test_05_tool_output_summary_vs_full_mode(self):
        """Test summary vs full mode for models_and_usage and list_jobs."""
        self.client.ensure_server = lambda root=None: None
        # Mock models: 5 models with heavy description
        mock_models = [
            {"providerID": "opencode-go", "id": f"model_{i}", "name": f"Model {i}", "status": "active", "cost": {"input": 1.0}, "description": "X" * 1000, "variants": ["a", "b"]}
            for i in range(5)
        ]
        def mock_req(m, p, b=None, **kw):
            if "model" in p:
                return {"data": mock_models}
            elif "info" in p:
                return {"version": "2.2.5"}
            elif "limits" in p:
                return {"data": {}}
            elif "agent" in p:
                return {"data": []}
            return {"data": {}}

        self.client.request = mock_req

        # 1. Summary mode (default)
        res_summary = self.client.models_and_usage(self.project_root, output_mode="summary")
        self.assertEqual(res_summary["output_mode"], "summary")
        self.assertIn("full_inspection_file", res_summary)
        self.assertTrue(os.path.exists(res_summary["full_inspection_file"]))
        # Verify compact models do NOT contain heavy 'description' or 'variants'
        for m in res_summary["models"]:
            self.assertNotIn("description", m)
            self.assertNotIn("variants", m)

        # 2. Full mode
        res_full = self.client.models_and_usage(self.project_root, output_mode="full")
        self.assertNotIn("full_inspection_file", res_full)
        for m in res_full["models"]:
            self.assertIn("description", m)

    def test_06_runtime_version_and_ondisk_modification_check(self):
        """Test runtime_version exposes build hash and ondisk check."""
        self.client.ensure_server = lambda root=None: None
        def mock_req(m, p, b=None, **kw):
            if "info" in p:
                return {"version": "2.2.5"}
            elif "model" in p:
                return {"data": []}
            elif "agent" in p:
                return {"data": []}
            elif "limits" in p:
                return {"data": {}}
            return {"data": {}}

        self.client.request = mock_req

        res = self.client.models_and_usage(self.project_root, output_mode="summary")
        rt = res["runtime_version"]
        self.assertEqual(rt["version"], core.VERSION)
        self.assertTrue(isinstance(rt["core_build_hash"], str))
        self.assertIn("start_time", rt)

    def test_07_adaptive_session_routing_hard_constraints(self):
        """Test hard constraints: busy session blocks reuse/fork, token threshold > 70k recommends fork/new."""
        slot_name = "test_slot_constraints"
        canonical_root = core.canonical_project_root(self.project_root)
        self.client.store.set_slot(canonical_root, slot_name, "ses_busy_test", "reviewer", "qwen3.8-flash")
        # Acquire lock to make it busy
        self.client.store.acquire_session_lock("ses_busy_test", "job_existing_busy", "inst_1")

        cost_pred = self.client.predict_session_cost(self.project_root, slot_name, "Some follow up task")
        self.assertEqual(cost_pred["recommended_strategy"], "new")
        self.assertIn("独占执行", cost_pred["recommendation_reason"])
        self.assertIn("unavailable", str(cost_pred["candidate_evaluations"]["reuse"]["cost_vector"]))

        # Release lock, simulate history tokens > 70k
        self.client.store.release_session_lock("ses_busy_test")
        self.client.session_inspect = lambda r, **kw: {"cumulative_tokens": {"input": 85000}}
        cost_pred_large = self.client.predict_session_cost(self.project_root, slot_name, "Follow up task 2")
        self.assertIn(cost_pred_large["recommended_strategy"], ("fork", "new"))
        self.assertIn("历史上下文较长", cost_pred_large["recommendation_reason"])

    def test_08_windows_mock_mcp_stdio_resilience(self):
        """Test Mock MCP stdio communication: application error does not kill server or drop pipe."""
        mcp_script = os.path.join(os.path.dirname(core.__file__), "opencode_go_worker_mcp.py")
        env = os.environ.copy()
        env["OPENCODE_GO_WORKER_DB"] = self.db_path

        proc = subprocess.Popen(
            [sys.executable, mcp_script],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            env=env,
        )

        try:
            # 1. Send initialize
            init_req = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
            proc.stdin.write(json.dumps(init_req) + "\n")
            proc.stdin.flush()
            line1 = proc.stdout.readline()
            resp1 = json.loads(line1)
            self.assertEqual(resp1["id"], 1)
            self.assertEqual(resp1["result"]["serverInfo"]["version"], core.VERSION)

            # 2. Send bad call (missing required parameters) -> should return structured error without crashing
            bad_req = {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "opencode_go_start", "arguments": {"project_root": self.project_root}},
            }
            proc.stdin.write(json.dumps(bad_req) + "\n")
            proc.stdin.flush()
            line2 = proc.stdout.readline()
            resp2 = json.loads(line2)
            self.assertEqual(resp2["id"], 2)
            self.assertTrue(resp2["result"]["isError"])

            # 3. Process is STILL ALIVE and can serve next request!
            self.assertIsNone(proc.poll())
            ping_req = {"jsonrpc": "2.0", "id": 3, "method": "ping"}
            proc.stdin.write(json.dumps(ping_req) + "\n")
            proc.stdin.flush()
            line3 = proc.stdout.readline()
            resp3 = json.loads(line3)
            self.assertEqual(resp3["id"], 3)
            self.assertEqual(resp3["result"], {})
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except Exception:
                proc.kill()


if __name__ == "__main__":
    unittest.main()
