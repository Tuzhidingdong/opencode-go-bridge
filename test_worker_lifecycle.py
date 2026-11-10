from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

_TEST_ROOT = tempfile.TemporaryDirectory(prefix="ocwm-lifecycle-test-")
os.environ["APPDATA"] = _TEST_ROOT.name

import opencode_go_worker_core as core
import opencode_go_worker_mcp as mcp


class MockLifecycleClient(core.OpenCodeClient):
    """Mock client simulating OpenCode server API and session states without external calls."""
    def __init__(self, db_path: Path | None = None):
        super().__init__()
        self.calls: list[tuple[str, str, Any]] = []
        self.active_sessions: dict[str, dict[str, Any]] = {}
        self.session_messages_map: dict[str, list[dict[str, Any]]] = {}
        self.session_permissions_map: dict[str, list[dict[str, Any]]] = {}
        self.interrupted_sessions: set[str] = set()
        self.next_prompt_error: Exception | None = None
        self.started_root: str | None = None

        if db_path:
            self.store.path = db_path
            self.store._init_db()

    def ensure_server(self, raw_project_root=None):
        if raw_project_root:
            self.started_root = core.canonical_project_root(raw_project_root)
        if not self.server:
            # Create a mock ServerHandle with pseudo PID
            dummy_proc = MagicMock()
            dummy_proc.pid = 99991
            dummy_proc.poll.return_value = None
            self.server = core.ServerHandle(
                process=dummy_proc,
                base_url="http://127.0.0.1:59999",
                username="opencode",
                password="mockpassword",
                log_path=Path(self.store.path.parent / "mock.log"),
                job_handle=None,
                server_pid=99991,
                server_start_time=1700000000.0,
            )
        return self.server

    def request(self, method: str, path: str, body: Any = None, timeout: int = core.API_TIMEOUT_SECONDS):
        self.calls.append((method, path, body))
        if path.startswith("/api/model?"):
            return {"data": [
                {"id": "mimo-v2.6-flash", "providerID": "opencode-go", "name": "MiMo Flash", "status": "active"},
            ]}
        if path.startswith("/api/experimental/usage/limits?"):
            return {"data": {"workspace": {"name": "Test"}, "go": {"weekly": {"percent": 0}}, "budgets": []}}
        if path == "/api/info":
            return {"data": {"ready": True, "version": "2.2.5"}}
        if path.startswith("/api/agent?"):
            return {"data": [
                {"id": "build", "name": "Build", "mode": "primary"},
                {"id": "explore", "name": "Explore", "mode": "subagent"},
            ]}
        if path == "/api/session/active":
            return {"data": {sid: info for sid, info in self.active_sessions.items()}}
        if path == "/api/session":
            sid = f"ses_mock_{uuid.uuid4().hex[:8]}"
            self.active_sessions[sid] = {"id": sid, "status": "created"}
            self.session_messages_map[sid] = []
            self.session_permissions_map[sid] = []
            return {"data": {"id": sid}}
        if "/prompt" in path:
            if self.next_prompt_error:
                err = self.next_prompt_error
                self.next_prompt_error = None
                raise err
            return {"data": {"status": "enqueued"}}
        if "/interrupt" in path:
            # Extract session id from path: /api/session/{sid}/interrupt
            parts = path.split("/")
            sid = parts[3]
            self.interrupted_sessions.add(sid)
            self.active_sessions.pop(sid, None)
            return {"data": {"interrupted": True}}
        if "/permission" in path:
            parts = path.split("/")
            sid = parts[3]
            return {"data": self.session_permissions_map.get(sid, [])}
        if "/message" in path:
            parts = path.split("/")
            sid = parts[3]
            return {"data": self.session_messages_map.get(sid, [])}
        if path.startswith("/api/session/ses_"):
            parts = path.split("/")
            sid = parts[3]
            return {"data": {"id": sid, "title": "Mock Session"}}
        raise AssertionError(f"Unexpected mock request: {method} {path}")


class WorkerLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(prefix="ocwm-lifecycle-case-")
        self.root = str(Path(self.temp_dir.name).resolve())
        self.db_path = Path(self.temp_dir.name) / "worker_jobs.sqlite3"
        self.client = MockLifecycleClient(db_path=self.db_path)

    def tearDown(self):
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    # 测试 1：正常完成 (Normal completion)
    def test_01_normal_completion(self):
        res = self.client.start_worker(
            raw_root=self.root,
            role="analyst",
            task="Analyze data files",
            model_id="mimo-v2.6-flash",
        )
        job_id = res["job_id"]
        sid = res["session_id"]
        self.assertEqual(res["state"], "submitted")

        # Initial check in database: state should be running (prompt enqueued)
        job = self.client.store.get(job_id)
        self.assertEqual(job["state"], "running")
        self.assertEqual(job["server_pid"], 99991)

        # Worker produces assistant completion message
        self.client.session_messages_map[sid] = [
            {
                "info": {
                    "role": "assistant",
                    "finish": "completed",
                    "tokens": {"input": 150, "output": 80, "reasoning": 10, "cache": {"read": 20, "write": 0}},
                    "cost": 0.0015,
                },
                "parts": [{"type": "text", "text": "Analysis complete: all metrics verified."}],
            }
        ]
        # Remove from active
        self.client.active_sessions.pop(sid, None)

        # wait_result returns final completed result
        final = self.client.wait_result(job_id, self.root, timeout_seconds=5)
        self.assertEqual(final["state"], "completed")
        self.assertEqual(len(final["messages"]), 1)
        self.assertIn("Analysis complete", final["messages"][0]["text"])
        self.assertEqual(final["usage_total_observed"]["input"], 150)
        self.assertEqual(final["usage_total_observed"]["output"], 80)
        self.assertEqual(final["usage_total_observed"]["reasoning"], 10)

    def test_01b_compact_result_preserves_full_result_cache(self):
        res = self.client.start_worker(
            raw_root=self.root,
            role="analyst",
            task="Return two assistant messages for compact/full result test.",
            model_id="mimo-v2.6-flash",
        )
        job_id = res["job_id"]
        sid = res["session_id"]
        self.client.session_messages_map[sid] = [
            {
                "info": {"id": "msg_1", "role": "assistant", "finish": "tool-calls", "tokens": {"input": 100, "output": 10}, "cost": 0},
                "parts": [{"type": "text", "text": "Intermediate note"}],
            },
            {
                "info": {"id": "msg_2", "role": "assistant", "finish": "completed", "tokens": {"input": 120, "output": 20, "reasoning": 5, "cache": {"read": 30, "write": 0}}, "cost": 0.001},
                "parts": [{"type": "text", "text": "Final verified result"}],
            },
        ]
        self.client.active_sessions.pop(sid, None)

        compact = self.client.job_result(job_id, self.root, output_mode="summary")
        self.assertEqual(compact["assistant_message_count"], 2)
        self.assertEqual(compact["omitted_assistant_messages"], 1)
        self.assertEqual(len(compact["messages"]), 1)
        self.assertEqual(compact["messages"][0]["text"], "Final verified result")
        self.assertEqual(compact["usage_total_observed"]["input"], 220)

        full = self.client.job_result(job_id, self.root, output_mode="full")
        self.assertEqual(len(full["messages"]), 2)
        self.assertEqual(full["messages"][0]["text"], "Intermediate note")
        self.assertEqual(full["output_mode"], "full")

    def test_01c_summary_truncates_long_final_text_but_full_mode_keeps_it(self):
        res = self.client.start_worker(
            raw_root=self.root,
            role="analyst",
            task="Check compact final text truncation and full retrieval.",
            model_id="mimo-v2.6-flash",
        )
        job_id = res["job_id"]
        sid = res["session_id"]
        long_text = "A" * 4000
        self.client.session_messages_map[sid] = [
            {
                "info": {
                    "id": "msg_long",
                    "role": "assistant",
                    "finish": "completed",
                    "tokens": {"input": 200, "output": 100},
                    "cost": 0.001,
                },
                "parts": [{"type": "text", "text": long_text}],
            }
        ]
        self.client.active_sessions.pop(sid, None)

        summary = self.client.job_result(job_id, self.root)
        self.assertTrue(summary["last_message_text_truncated"])
        self.assertIsNone(summary["last_assistant_excerpt"])
        self.assertTrue(summary["messages"][0]["text"].startswith("A" * 1600))
        self.assertIn("[truncated;", summary["messages"][0]["text"])

        full = self.client.job_result(job_id, self.root, output_mode="full")
        self.assertEqual(full["messages"][0]["text"], long_text)
        self.assertEqual(full["output_mode"], "full")

    # 测试 2：模型请求失败 (Model request failure / auth expired)
    def test_02_model_request_failure(self):
        self.client.next_prompt_error = core.BridgeError(
            "[AUTH_KEY_EXPIRED] " + core.AUTH_KEY_EXPIRED_MESSAGE,
            category="AUTH_KEY_EXPIRED",
            user_message=core.AUTH_KEY_EXPIRED_MESSAGE,
            status_code=401,
        )
        with self.assertRaises(core.BridgeError) as ctx:
            self.client.start_worker(
                raw_root=self.root,
                role="writer",
                task="Write draft",
                model_id="mimo-v2.6-flash",
            )
        self.assertEqual(ctx.exception.category, "AUTH_KEY_EXPIRED")

        jobs = self.client.store.list(self.root)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["state"], "failed")
        self.assertIn("AUTH_KEY_EXPIRED", jobs[0]["last_error"])

        # job_result reflects failure accurately
        res = self.client.job_result(jobs[0]["job_id"], self.root)
        self.assertEqual(res["state"], "failed")
        self.assertEqual(res["error_category"], "AUTH_KEY_EXPIRED")

    # 测试 3：Worker 超时 (Worker wait timeout)
    def test_03_worker_wait_timeout(self):
        res = self.client.start_worker(
            raw_root=self.root,
            role="general",
            task="Long background calculation",
            model_id="mimo-v2.6-flash",
        )
        job_id = res["job_id"]
        sid = res["session_id"]
        # Keep worker in active_sessions
        self.assertIn(sid, self.client.active_sessions)

        # wait_result with short timeout (1 second)
        wait_res = self.client.wait_result(job_id, self.root, timeout_seconds=1, poll_interval=0.5)
        self.assertTrue(wait_res.get("timed_out"))
        self.assertEqual(wait_res.get("state"), "running")
        self.assertEqual(wait_res.get("wait_status"), "timeout_expired")
        # Worker is still alive in store and active sessions
        job = self.client.store.get(job_id)
        self.assertEqual(job["state"], "running")

    # 测试 4：用户取消 (User cancellation)
    def test_04_user_cancellation(self):
        res = self.client.start_worker(
            raw_root=self.root,
            role="reviewer",
            task="Review chapter",
            model_id="mimo-v2.6-flash",
        )
        job_id = res["job_id"]
        sid = res["session_id"]
        self.assertIn(sid, self.client.active_sessions)

        cancel_res = self.client.cancel_worker(job_id, self.root)
        self.assertTrue(cancel_res["verified_stopped"])
        self.assertEqual(cancel_res["state"], "interrupted")
        self.assertNotIn(sid, self.client.active_sessions)

        job = self.client.store.get(job_id)
        self.assertEqual(job["state"], "interrupted")

    # 测试 5：MCP 客户端断开 (Client disconnect / state preserved in DB)
    def test_05_mcp_client_disconnect_preserves_task(self):
        # Simulate worker start being recorded right before simulated disconnect
        res = self.client.start_worker(
            raw_root=self.root,
            role="writer",
            task="Draft text",
            model_id="mimo-v2.6-flash",
        )
        job_id = res["job_id"]

        # Simulate MCP manager disconnect / recreation
        new_client = MockLifecycleClient(db_path=self.db_path)
        persisted = new_client.store.get(job_id)
        self.assertIsNotNone(persisted)
        self.assertEqual(persisted["job_id"], job_id)
        self.assertEqual(persisted["project_root"], self.root)

    # 测试 6：Manager 异常退出 (Job Object configuration & process tracking)
    def test_06_manager_abnormal_exit_process_tracking(self):
        # Verify Job Object helper runs cleanly on Windows or returns None gracefully on other OS
        job_obj = core._create_kill_on_close_job_object()
        if os.name == "nt":
            self.assertIsNotNone(job_obj)
            core._close_job_handle(job_obj)

    # 测试 7：Manager 重启后发现残留 running (Restart finds stale running)
    def test_07_restart_reconciliation_cleans_stale_running(self):
        # Manually inject a stale running job with a dead PID into the database
        now = time.time()
        self.client.store.add({
            "job_id": "job_stale_123",
            "session_id": "ses_stale_123",
            "project_root": self.root,
            "role": "writer",
            "model_id": "mimo-v2.6-flash",
            "title": "Stale job",
            "state": "running",
            "server_pid": 99998,
            "server_start_time": 1000.0,
            "created_at": now - 3600,
            "updated_at": now - 3600,
        })
        self.assertEqual(self.client.store.get("job_stale_123")["state"], "running")

        # Mock process check returning dead process for 99998
        with patch("opencode_go_worker_core._is_process_alive_and_matching", return_value=False):
            reconcile_report = self.client.reconcile_unfinalized_jobs()
            self.assertGreaterEqual(reconcile_report["reconciled_count"], 1)

            stale_job = self.client.store.get("job_stale_123")
            self.assertEqual(stale_job["state"], "interrupted")
            self.assertIn("PROCESS_TERMINATED", stale_job["last_error"])

    # 测试 8：Worker 子进程残留 / PID 复用防误杀 (Subprocess / PID reuse detection)
    def test_08_pid_reuse_detection(self):
        # Current process PID
        my_pid = os.getpid()
        real_create_time = core._get_process_create_time(my_pid)
        self.assertIsNotNone(real_create_time)

        # Same PID with exact start time -> matches
        self.assertTrue(core._is_process_alive_and_matching(my_pid, real_create_time))

        # Same PID with vastly different start time (PID reused) -> rejected!
        fake_old_start_time = real_create_time - 7200.0
        self.assertFalse(core._is_process_alive_and_matching(my_pid, fake_old_start_time))

    # 测试 9：数据库写入失败处理 (Database write failure handling)
    def test_09_database_write_failure_during_start(self):
        # Simulate database locked or inaccessible during store.add
        orig_add = self.client.store.add
        self.client.store.add = MagicMock(side_effect=sqlite3.OperationalError("database is locked"))

        with self.assertRaises(sqlite3.OperationalError):
            self.client.start_worker(
                raw_root=self.root,
                role="analyst",
                task="Test DB lock",
                model_id="mimo-v2.6-flash",
            )
        self.client.store.add = orig_add

    # 测试 10：重复取消和重复获取 result (Repeated cancel and idempotent result)
    def test_10_repeated_cancel_and_idempotent_result(self):
        res = self.client.start_worker(
            raw_root=self.root,
            role="reviewer",
            task="Idempotency test",
            model_id="mimo-v2.6-flash",
        )
        job_id = res["job_id"]
        sid = res["session_id"]

        # Cancel #1
        c1 = self.client.cancel_worker(job_id, self.root)
        self.assertEqual(c1["state"], "interrupted")

        # Cancel #2 (Repeated cancel) -> returns immediately with terminal state
        c2 = self.client.cancel_worker(job_id, self.root)
        self.assertEqual(c2["state"], "interrupted")
        self.assertIn("already in terminal state", c2.get("note", ""))

        # Result #1
        r1 = self.client.job_result(job_id, self.root)
        self.assertEqual(r1["state"], "interrupted")

        # Result #2 (Repeated result) -> cached terminal result returned
        r2 = self.client.job_result(job_id, self.root)
        self.assertEqual(r2["state"], "interrupted")
        self.assertEqual(r1, r2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
