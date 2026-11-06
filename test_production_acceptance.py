from __future__ import annotations

import http.client
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import uuid
from pathlib import Path
from unittest.mock import MagicMock

# Isolated temp APPDATA
_TEST_ROOT = tempfile.TemporaryDirectory(prefix="ocwm-prod-acceptance-")
os.environ["APPDATA"] = _TEST_ROOT.name

import opencode_go_worker_core as core
import opencode_go_worker_mcp as mcp


class MockSubmissionClient(core.OpenCodeClient):
    """Client for testing prompt submission uncertainties."""

    def __init__(self, db_path: Path):
        super().__init__()
        self.store.path = db_path
        self.store._init_db()
        self.prompt_failure_mode: str | None = None
        self.active_sessions: dict[str, dict[str, Any]] = {}
        self.session_messages_map: dict[str, list[dict[str, Any]]] = {}

    def ensure_server(self, raw_project_root=None):
        if not self.server:
            dummy_proc = MagicMock()
            dummy_proc.pid = 99991
            dummy_proc.poll.return_value = None
            self.server = core.ServerHandle(
                process=dummy_proc,
                base_url="http://127.0.0.1:59991",
                username="opencode",
                password="mockpassword",
                log_path=Path(self.store.path.parent / "mock.log"),
                job_handle=None,
                server_pid=99991,
                server_start_time=1700000000.0,
            )
        return self.server

    def request(self, method: str, path: str, body: Any = None, timeout: int = core.API_TIMEOUT_SECONDS):
        if path.startswith("/api/model?"):
            return {"data": [{"id": "mimo-v2.6-flash", "providerID": "opencode-go", "name": "MiMo", "status": "active", "limit": {"context": 128000}}]}
        if path.startswith("/api/experimental/usage/limits?"):
            return {"data": {"workspace": {"name": "Test"}, "go": {"weekly": {"percent": 0}}, "budgets": []}}
        if path == "/api/info":
            return {"data": {"ready": True, "version": "2.2.5"}}
        if path.startswith("/api/agent?"):
            return {"data": [{"id": "build", "name": "Build", "mode": "primary"}, {"id": "explore", "name": "Explore", "mode": "subagent"}]}
        if path == "/api/session":
            sid = f"ses_{uuid.uuid4().hex[:8]}"
            self.session_messages_map[sid] = []
            return {"data": {"id": sid}}
        if "/fork" in path:
            sid = f"ses_fork_{uuid.uuid4().hex[:8]}"
            self.session_messages_map[sid] = []
            return {"data": {"id": sid}}
        if path == "/api/session/active":
            return {"data": dict(self.active_sessions)}
        if "/permission" in path:
            return {"data": []}
        if "/message" in path:
            sid = path.split("/")[3]
            return {"data": self.session_messages_map.get(sid, [])}
        if path.startswith("/api/session/ses_") and "/prompt" not in path:
            sid = path.split("/")[3]
            return {"data": {"id": sid, "title": "Mock Session"}}

        if "/prompt" in path:
            sid = path.split("/")[3]
            if self.prompt_failure_mode == "connection_refused":
                raise urllib.error.URLError(ConnectionRefusedError(10061, "No connection could be made because the target machine actively refused it"))
            elif self.prompt_failure_mode == "timeout":
                raise TimeoutError("HTTP request timed out while waiting for prompt response")
            elif self.prompt_failure_mode == "remote_disconnected":
                # Server got the prompt, but socket closed during read
                self.active_sessions[sid] = {"id": sid}
                raise http.client.RemoteDisconnected("Remote end closed connection without response")
            else:
                self.active_sessions[sid] = {"id": sid}
                return {"data": {"status": "enqueued"}}

        raise AssertionError(f"Unexpected mock request: {method} {path}")


class ProductionAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ocwm-prod-test-")
        self.root = str(Path(self.tmp.name).resolve())
        self.db_path = Path(self.tmp.name) / "worker_jobs.sqlite3"
        self.client = MockSubmissionClient(db_path=self.db_path)

    def tearDown(self):
        try:
            self.tmp.cleanup()
        except Exception:
            pass

    # =========================================================================
    # 环节 1: Prompt 提交不确定性验证
    # =========================================================================

    def test_01_submission_connection_refused_definite_failed(self):
        """1. 请求发出前连接失败 (ConnectionRefused): 明确标记 failed，释放 session 锁。"""
        self.client.prompt_failure_mode = "connection_refused"
        with self.assertRaises(core.BridgeError) as ctx:
            self.client.start_worker(
                raw_root=self.root,
                role="writer",
                task="Test prompt connection refused",
                model_id="mimo-v2.6-flash",
                session_strategy="new",
            )
        self.assertEqual(ctx.exception.category, "NETWORK_ERROR")

        # Database job should be marked failed
        jobs = self.client.store.list(self.root, limit=10)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["state"], "failed")

        # Session lock must be released
        sid = jobs[0]["session_id"]
        is_busy, _ = self.client.is_session_busy(sid)
        self.assertFalse(is_busy)

    def test_02_submission_timeout_becomes_submission_unknown(self):
        """2. OpenCode 收到请求后响应超时: 不得标记 failed，标记为 submission_unknown，不释放锁，严禁自动重试。"""
        self.client.prompt_failure_mode = "timeout"
        with self.assertRaises(core.BridgeError) as ctx:
            self.client.start_worker(
                raw_root=self.root,
                role="writer",
                worker_slot="Slot_A",
                task="Test timeout prompt",
                model_id="mimo-v2.6-flash",
                session_strategy="new",
            )
        self.assertEqual(ctx.exception.category, "SUBMISSION_UNKNOWN")

        jobs = self.client.store.list(self.root, limit=10)
        self.assertEqual(len(jobs), 1)
        job = jobs[0]
        # Must be submission_unknown, NEVER failed!
        self.assertEqual(job["state"], "submission_unknown")

        # Attempting to start another job on the same session/slot must be strictly blocked!
        with self.assertRaises(core.BridgeError) as ctx_dup:
            self.client.start_worker(
                raw_root=self.root,
                role="writer",
                worker_slot="Slot_A",
                task="Duplicate retry attempt",
                model_id="mimo-v2.6-flash",
                session_strategy="reuse",
            )
        err_msg = str(ctx_dup.exception)
        self.assertTrue(
            "当前正在执行 Job" in err_msg or "submission_unknown" in err_msg,
            f"Expected session lock or submission_unknown rejection, got: {err_msg}"
        )

    def test_03_submission_disconnected_reconciled_to_running_when_active(self):
        """3. 模型执行过程中连接中断，但服务端实际已启动: 核实后安全接入 running，不伪造失败。"""
        self.client.prompt_failure_mode = "remote_disconnected"
        # RemoteDisconnected triggers immediate check on active sessions
        res = self.client.start_worker(
            raw_root=self.root,
            role="writer",
            worker_slot="Slot_B",
            task="Test disconnected but started",
            model_id="mimo-v2.6-flash",
            session_strategy="new",
        )
        job_id = res["job_id"]
        # Verified active -> state converged to running!
        job = self.client.store.get(job_id)
        self.assertEqual(job["state"], "running")

    def test_04_manager_restart_reconciles_submission_unknown(self):
        """4. Manager 重启后发现提交状态不确定: 若确认服务进程已终止，安全收敛至 interrupted。"""
        # Create a job directly in submission_unknown state simulating crashed session
        job_id = "job_test_unknown_crash"
        sid = "ses_unknown_crash"
        self.client.store.add({
            "job_id": job_id,
            "session_id": sid,
            "project_root": self.root,
            "role": "analyst",
            "model_id": "mimo-v2.6-flash",
            "title": "Unknown Job",
            "state": "submission_unknown",
            "server_pid": 99998,
            "server_start_time": 100000.0,  # Dead PID
        })

        # New manager instance restarts and runs reconcile_unfinalized_jobs()
        restart_client = MockSubmissionClient(db_path=self.db_path)
        reconciled = restart_client.reconcile_unfinalized_jobs()
        self.assertGreaterEqual(reconciled["reconciled_count"], 1)

        # Verify reconciliation converged to interrupted
        job = restart_client.store.get(job_id)
        self.assertEqual(job["state"], "interrupted")

    def test_05_idempotent_reconciliation_and_no_ghost_token_burn(self):
        """5. 重复提交和重复恢复防护: 保证幂等，绝不启动重复模型任务。"""
        # Reconcile multiple times
        res1 = self.client.reconcile_unfinalized_jobs()
        res2 = self.client.reconcile_unfinalized_jobs()
        self.assertEqual(res2["reconciled_count"], 0)

    # =========================================================================
    # 环节 2: Windows Job Object 所有权隔离验证 (保护外部进程如 PID 27444)
    # =========================================================================

    def test_06_job_object_process_isolation_and_external_protection(self):
        """6. Windows Job Object: 只杀死自己名下的子进程，外部 opencode 绝不误杀。"""
        if os.name != "nt":
            self.skipTest("Windows Job Object is specific to Windows NT")

        # 1. Start an independent fake external process (simulating PID 27444)
        ext_proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        ext_pid = ext_proc.pid

        # 2. Start a fake manager worker process inside a Job Object
        job_obj = core._create_kill_on_close_job_object()
        self.assertIsNotNone(job_obj)

        worker_proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        worker_pid = worker_proc.pid

        # Assign worker to Job Object
        assigned = core._assign_process_to_job(job_obj, worker_proc)
        self.assertTrue(assigned)

        try:
            # Both processes are initially running
            self.assertIsNone(worker_proc.poll())
            self.assertIsNone(ext_proc.poll())

            # 3. Close the Job Object handle (simulating Manager exit/crash)
            core._close_job_handle(job_obj)
            job_obj = None

            # Give Windows kernel a brief moment to terminate processes in the Job Object
            time.sleep(0.5)

            # 4. Worker process inside Job Object MUST BE TERMINATED
            self.assertIsNotNone(worker_proc.poll())

            # 5. External process OUTSIDE Job Object MUST REMAIN COMPLETELY ALIVE!
            self.assertIsNone(ext_proc.poll())
            self.assertTrue(core._is_process_alive_and_matching(ext_pid, core._get_process_create_time(ext_pid)))
        finally:
            # Clean up external process
            try:
                ext_proc.terminate()
                ext_proc.wait(timeout=2)
            except Exception:
                pass

    def test_07_anti_pid_reuse_protection(self):
        """7. 进程创建时间指纹防 PID 复用误杀。"""
        current_pid = os.getpid()
        real_start_time = core._get_process_create_time(current_pid)
        self.assertIsNotNone(real_start_time)

        # Matching PID and matching start time -> True
        self.assertTrue(core._is_process_alive_and_matching(current_pid, real_start_time))

        # Matching PID but DIFFERENT start time (simulating PID recycled by Windows) -> MUST BE FALSE!
        fake_old_start_time = real_start_time - 10000.0
        self.assertFalse(core._is_process_alive_and_matching(current_pid, fake_old_start_time))

    # =========================================================================
    # 环节 3: Adaptive Session Routing 真实工具闭环验证
    # =========================================================================

    def test_08_adaptive_session_routing_full_tool_call_loop(self):
        """8. 全链路验证: inspect -> predict_cost -> start -> wait_result -> update_summary -> reuse."""
        mcp.CLIENT = self.client

        # 1. inspect slot (initially empty)
        res_inspect = mcp.handle_tool("opencode_go_session_inspect", {
            "project_root": self.root,
            "worker_slot": "Research_Lead",
        })
        self.assertFalse(res_inspect["isError"])
        data_inspect = json.loads(res_inspect["content"][0]["text"])
        self.assertEqual(data_inspect["data_quality"], "missing_session")

        # 2. predict cost (candidate: new)
        res_pred = mcp.handle_tool("opencode_go_predict_session_cost", {
            "project_root": self.root,
            "worker_slot": "Research_Lead",
            "task": "Formulate new catalyst",
        })
        self.assertFalse(res_pred["isError"])
        data_pred = json.loads(res_pred["content"][0]["text"])
        self.assertEqual(data_pred["recommended_strategy"], "new")

        # 3. start worker with strategy='new'
        res_start = mcp.handle_tool("opencode_go_start", {
            "project_root": self.root,
            "role": "writer",
            "worker_slot": "Research_Lead",
            "task": "Formulate new catalyst",
            "model_id": "mimo-v2.6-flash",
            "session_strategy": "new",
            "decision_reason": "Cold start for research paper",
            "estimated_cost": {"input_estimate": 500, "output_estimate": 200},
            "prediction_confidence": 0.9,
        })
        self.assertFalse(res_start["isError"])
        data_start = json.loads(res_start["content"][0]["text"])
        job1_id = data_start["job_id"]
        sid1 = data_start["session_id"]

        # Simulate job completion
        self.client.active_sessions.pop(sid1, None)
        self.client.session_messages_map[sid1] = [{
            "info": {"role": "assistant", "finish": "completed", "tokens": {"input": 480, "output": 210, "cache": {"read": 0, "write": 480}}, "cost": 0.0025},
            "parts": [{"type": "text", "text": "Catalyst formulation completed."}],
        }]

        # 4. wait_result retrieves final result
        res_wait = mcp.handle_tool("opencode_go_wait_result", {
            "project_root": self.root,
            "job_id": job1_id,
            "timeout_seconds": 10,
        })
        self.assertFalse(res_wait["isError"])
        data_wait = json.loads(res_wait["content"][0]["text"])
        self.assertEqual(data_wait["state"], "completed")

        # 5. update session summary with persistent memory
        res_sum = mcp.handle_tool("opencode_go_update_session_summary", {
            "project_root": self.root,
            "worker_slot": "Research_Lead",
            "summary": "Verified formula: 15% Zn-doped composite is optimal.",
        })
        self.assertFalse(res_sum["isError"])

        # 6. inspect slot again: verify slot has summary, session_id, and cumulative tokens!
        res_inspect2 = mcp.handle_tool("opencode_go_session_inspect", {
            "project_root": self.root,
            "worker_slot": "Research_Lead",
        })
        data_inspect2 = json.loads(res_inspect2["content"][0]["text"])
        self.assertEqual(data_inspect2["session_id"], sid1)
        self.assertEqual(data_inspect2["slot_summary"], "Verified formula: 15% Zn-doped composite is optimal.")
        self.assertEqual(data_inspect2["cumulative_tokens"]["completed_jobs"], 1)
        self.assertEqual(data_inspect2["cumulative_tokens"]["input"], 480)

        # 7. start job 2 with strategy='reuse'
        res_start2 = mcp.handle_tool("opencode_go_start", {
            "project_root": self.root,
            "role": "writer",
            "worker_slot": "Research_Lead",
            "task": "Synthesize results based on previous formulation",
            "model_id": "mimo-v2.6-flash",
            "session_strategy": "reuse",
            "decision_reason": "Continuity task reusing session",
        })
        data_start2 = json.loads(res_start2["content"][0]["text"])
        job2_id = data_start2["job_id"]
        sid2 = data_start2["session_id"]

        self.assertNotEqual(job1_id, job2_id)
        self.assertEqual(sid1, sid2)  # Reused identical session!

        # 8. Check decision ledger feedback loop
        dec1 = self.client.store.get_decision_by_job(job1_id)
        self.assertEqual(dec1["task_outcome"], "completed")
        self.assertIsNotNone(dec1["actual_usage"])
        actual_tokens = json.loads(dec1["actual_usage"])
        self.assertEqual(actual_tokens["input"], 480)
        self.assertEqual(actual_tokens["output"], 210)


if __name__ == "__main__":
    unittest.main(verbosity=2)
