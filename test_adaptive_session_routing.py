from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import MagicMock

_TEST_ROOT = tempfile.TemporaryDirectory(prefix="ocwm-routing-test-")
os.environ["APPDATA"] = _TEST_ROOT.name

import opencode_go_worker_core as core
import opencode_go_worker_mcp as mcp


class MockRoutingClient(core.OpenCodeClient):
    def __init__(self, db_path: Path | None = None):
        super().__init__()
        self.calls: list[tuple[str, str, Any]] = []
        self.active_sessions: dict[str, dict[str, Any]] = {}
        self.session_messages_map: dict[str, list[dict[str, Any]]] = {}
        self.session_permissions_map: dict[str, list[dict[str, Any]]] = {}
        self.fork_counter = 0

        if db_path:
            self.store.path = db_path
            self.store._init_db()

    def ensure_server(self, raw_project_root=None):
        if not self.server:
            dummy_proc = MagicMock()
            dummy_proc.pid = 88881
            dummy_proc.poll.return_value = None
            self.server = core.ServerHandle(
                process=dummy_proc,
                base_url="http://127.0.0.1:58888",
                username="opencode",
                password="mockpassword",
                log_path=Path(self.store.path.parent / "mock.log"),
                job_handle=None,
                server_pid=88881,
                server_start_time=1700000000.0,
            )
        return self.server

    def request(self, method: str, path: str, body: Any = None, timeout: int = core.API_TIMEOUT_SECONDS):
        self.calls.append((method, path, body))
        if path.startswith("/api/model?"):
            return {"data": [
                {"id": "mimo-v2.6-flash", "providerID": "opencode-go", "name": "MiMo Flash", "status": "active", "limit": {"context": 128000}},
                {"id": "kimi-k3.1-pro", "providerID": "opencode-go", "name": "Kimi Pro", "status": "active", "limit": {"context": 256000}},
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
        if "/interrupt" in path:
            parts = path.split("/")
            sid = parts[3]
            self.active_sessions.pop(sid, None)
            return {"data": True}
        if path == "/api/session/active":
            res = {}
            for sid, info in list(self.active_sessions.items()):
                msgs = self.session_messages_map.get(sid, [])
                last_finish = None
                for m in reversed(msgs):
                    if m.get("info", {}).get("role") == "assistant":
                        last_finish = m.get("info", {}).get("finish")
                        break
                if last_finish in ("stop", "completed", "end_turn", "failed", "error"):
                    self.active_sessions.pop(sid, None)
                else:
                    res[sid] = info
            return {"data": res}
        if path == "/api/session":
            sid = f"ses_fresh_{uuid.uuid4().hex[:8]}"
            self.session_messages_map[sid] = []
            return {"data": {"id": sid}}
        if "/fork" in path:
            self.fork_counter += 1
            sid = f"ses_forked_{self.fork_counter}_{uuid.uuid4().hex[:6]}"
            self.session_messages_map[sid] = []
            return {"data": {"id": sid}}
        if "/prompt" in path:
            parts = path.split("/")
            sid = parts[3]
            self.active_sessions[sid] = {"id": sid}
            return {"data": {"status": "enqueued"}}
        if "/permission" in path:
            return {"data": []}
        if "/message" in path:
            parts = path.split("/")
            sid = parts[3]
            return {"data": self.session_messages_map.get(sid, [])}
        if path.startswith("/api/session/ses_"):
            parts = path.split("/")
            sid = parts[3]
            return {"data": {"id": sid, "title": "Session Mock"}}
        raise AssertionError(f"Unexpected mock request: {method} {path}")


class AdaptiveSessionRoutingTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir1 = tempfile.TemporaryDirectory(prefix="ocwm-proj1-")
        self.temp_dir2 = tempfile.TemporaryDirectory(prefix="ocwm-proj2-")
        self.root1 = str(Path(self.temp_dir1.name).resolve())
        self.root2 = str(Path(self.temp_dir2.name).resolve())
        self.db_path = Path(self.temp_dir1.name) / "worker_jobs.sqlite3"
        self.client = MockRoutingClient(db_path=self.db_path)

    def tearDown(self):
        try:
            self.temp_dir1.cleanup()
            self.temp_dir2.cleanup()
        except Exception:
            pass

    # 1. 同一项目、同一角色的连续任务能够复用 Session
    def test_01_same_project_slot_reuse_session(self):
        # Job 1: Strategy New
        res1 = self.client.start_worker(
            raw_root=self.root1,
            role="writer",
            worker_slot="Lead_Writer",
            task="Draft Section 1",
            model_id="mimo-v2.6-flash",
            session_strategy="new",
        )
        sid1 = res1["session_id"]
        job1_id = res1["job_id"]
        self.assertTrue(sid1.startswith("ses_fresh_"))

        # Finish Job 1
        self.client.session_messages_map[sid1] = [{
            "info": {"role": "assistant", "finish": "completed", "tokens": {"input": 500, "output": 200}},
            "parts": [{"type": "text", "text": "Draft 1 completed"}],
        }]
        self.client.job_result(job1_id, self.root1)

        # Job 2: Strategy Reuse
        res2 = self.client.start_worker(
            raw_root=self.root1,
            role="writer",
            worker_slot="Lead_Writer",
            task="Continue with Section 2",
            model_id="mimo-v2.6-flash",
            session_strategy="reuse",
        )
        sid2 = res2["session_id"]
        job2_id = res2["job_id"]

        self.assertNotEqual(job1_id, job2_id)
        self.assertEqual(sid1, sid2)  # Successfully reused!
        self.assertEqual(res2["worker_slot"], "Lead_Writer")

    # 2. 不同项目不会错误复用 Session
    def test_02_cross_project_isolation(self):
        # Create session in Proj 1
        res1 = self.client.start_worker(
            raw_root=self.root1,
            role="reviewer",
            worker_slot="Reviewer_A",
            task="Review Proj 1",
            model_id="mimo-v2.6-flash",
            session_strategy="new",
        )
        sid1 = res1["session_id"]
        self.client.job_result(res1["job_id"], self.root1)

        # Try to reuse in Proj 2 with same slot name -> must fail because Proj 2 has no session!
        with self.assertRaises(core.BridgeError) as ctx:
            self.client.start_worker(
                raw_root=self.root2,
                role="reviewer",
                worker_slot="Reviewer_A",
                task="Review Proj 2",
                model_id="mimo-v2.6-flash",
                session_strategy="reuse",
            )
        self.assertIn("暂无历史 Session 可复用", str(ctx.exception))

    # 3. 任务方向明显变化时 Anti 能选择新建
    def test_03_anti_selects_new_strategy(self):
        res1 = self.client.start_worker(
            raw_root=self.root1,
            role="analyst",
            worker_slot="Analyst_A",
            task="Analyze chemistry experiments",
            model_id="mimo-v2.6-flash",
            session_strategy="new",
        )
        sid1 = res1["session_id"]
        self.client.job_result(res1["job_id"], self.root1)

        # Direction changes: Anti decides to use strategy="new"
        res2 = self.client.start_worker(
            raw_root=self.root1,
            role="analyst",
            worker_slot="Analyst_A",
            task="Brand new direction: economic cost model",
            model_id="mimo-v2.6-flash",
            session_strategy="new",
            decision_reason="Research direction pivoted to financial analysis",
        )
        sid2 = res2["session_id"]
        self.assertNotEqual(sid1, sid2)  # New independent session created

        # Slot mapping updated to the new session
        slot = self.client.store.get_slot(self.root1, "Analyst_A")
        self.assertEqual(slot["current_session_id"], sid2)

    # 4. 任务分支时能够选择 Fork
    def test_04_fork_strategy_creates_branch(self):
        res1 = self.client.start_worker(
            raw_root=self.root1,
            role="writer",
            worker_slot="Exploration_Lead",
            task="Base formulation",
            model_id="mimo-v2.6-flash",
            session_strategy="new",
        )
        sid1 = res1["session_id"]
        # Simulate a completed parent Job before requesting Fork.
        self.client.session_messages_map[sid1] = [{
            "info": {"role": "assistant", "finish": "completed", "tokens": {"input": 120, "output": 30}},
            "parts": [{"type": "text", "text": "Base formulation completed"}],
        }]
        self.client.job_result(res1["job_id"], self.root1)

        # Anti forks to explore an alternative hypothesis
        res2 = self.client.start_worker(
            raw_root=self.root1,
            role="writer",
            worker_slot="Alternative_Hypothesis_Branch",
            session_id=sid1,  # fork from parent sid1
            task="Explore alternative formula with high dopant concentration",
            model_id="mimo-v2.6-flash",
            session_strategy="fork",
            decision_reason="Forking to evaluate side hypothesis without polluting primary line",
        )
        sid2 = res2["session_id"]
        self.assertTrue(sid2.startswith("ses_forked_"))
        self.assertNotEqual(sid1, sid2)

    # 5. 历史上下文过长时能够考虑压缩或轮换
    def test_05_long_context_recommends_fork_or_new(self):
        # Start a slot and simulate massive token consumption
        res = self.client.start_worker(
            raw_root=self.root1,
            role="writer",
            worker_slot="Heavy_Writer",
            task="Chapter 1",
            model_id="mimo-v2.6-flash",
            session_strategy="new",
        )
        sid = res["session_id"]
        self.client.session_messages_map[sid] = [{
            "info": {"role": "assistant", "finish": "completed", "tokens": {"input": 95000, "output": 2000}},
            "parts": [{"type": "text", "text": "Heavy chapter"}],
        }]
        self.client.job_result(res["job_id"], self.root1)

        # Inspect slot
        inspection = self.client.session_inspect(self.root1, worker_slot="Heavy_Writer")
        self.assertIsNotNone(inspection["context_warning"])
        self.assertIn("70% 以上", inspection["context_warning"])

        # Cost predictor recommends fork/new over reuse for bloated context
        prediction = self.client.predict_session_cost(
            self.root1,
            worker_slot="Heavy_Writer",
            task="Next chapter",
        )
        self.assertEqual(prediction["recommended_strategy"], "fork")
        self.assertIn("历史上下文较长", prediction["recommendation_reason"])

    # 6. 同一 Session 并发调用受到控制 (Session lock)
    def test_06_session_level_concurrency_lock(self):
        res1 = self.client.start_worker(
            raw_root=self.root1,
            role="writer",
            worker_slot="Shared_Slot",
            task="Task 1",
            model_id="mimo-v2.6-flash",
            session_strategy="new",
        )
        sid = res1["session_id"]
        # Job 1 is still running (terminal result not yet fetched)

        # Attempting to start Job 2 on the same session while Job 1 is running
        with self.assertRaises(core.BridgeError) as ctx:
            self.client.start_worker(
                raw_root=self.root1,
                role="writer",
                worker_slot="Shared_Slot",
                task="Task 2 conflicting",
                model_id="mimo-v2.6-flash",
                session_strategy="reuse",
            )
        self.assertIn("当前正在执行 Job", str(ctx.exception))
        self.assertIn("严禁并发执行", str(ctx.exception))

        # Once Job 1 finishes, reuse is permitted
        self.client.session_messages_map[sid] = [{
            "info": {"role": "assistant", "finish": "completed", "tokens": {"input": 100, "output": 50}},
            "parts": [{"type": "text", "text": "Task 1 completed"}],
        }]
        self.client.job_result(res1["job_id"], self.root1)
        res2 = self.client.start_worker(
            raw_root=self.root1,
            role="writer",
            worker_slot="Shared_Slot",
            task="Task 2 after job 1 completion",
            model_id="mimo-v2.6-flash",
            session_strategy="reuse",
        )
        self.assertEqual(res2["session_id"], sid)

    # 7. Manager 重启后恢复 Session 映射
    def test_07_manager_restart_restores_slot_mappings(self):
        res = self.client.start_worker(
            raw_root=self.root1,
            role="analyst",
            worker_slot="Persistent_Analyst",
            task="Task 1",
            model_id="mimo-v2.6-flash",
            session_strategy="new",
        )
        sid = res["session_id"]
        self.client.job_result(res["job_id"], self.root1)

        # Re-instantiate a fresh manager client using same db
        new_client = MockRoutingClient(db_path=self.db_path)
        slot = new_client.store.get_slot(self.root1, "Persistent_Analyst")
        self.assertIsNotNone(slot)
        self.assertEqual(slot["current_session_id"], sid)
        self.assertEqual(slot["role"], "analyst")

    # 8. Token 历史数据缺失时能够安全决策
    def test_08_missing_token_history_safe_fallback(self):
        # Inspect a slot that doesn't exist
        res = self.client.session_inspect(self.root1, worker_slot="Non_Existent_Slot")
        self.assertEqual(res["data_quality"], "missing_session")
        self.assertEqual(res["session_id"], "unavailable")

        pred = self.client.predict_session_cost(self.root1, worker_slot="Non_Existent_Slot", task="Test task")
        self.assertEqual(pred["recommended_strategy"], "new")
        self.assertTrue(any("暂无历史 Session" in f for f in pred["uncertainty_factors"]))

    # 9. 模型价格配置不完整时不会产生虚假的精确成本
    def test_09_incomplete_pricing_uses_token_vector(self):
        pred = self.client.predict_session_cost(self.root1, worker_slot="Any_Slot", task="Estimate vectors")
        new_eval = pred["candidate_evaluations"]["new"]
        cost_vec = new_eval["cost_vector"]
        self.assertIn("expected_uncached_input", cost_vec)
        self.assertIn("expected_cache_read", cost_vec)
        self.assertIn("rework_risk_factor", cost_vec)

    # 10. Job 失败、取消或超时后 Session 注册关系仍然一致
    def test_10_slot_retained_on_job_failure_or_cancel(self):
        res = self.client.start_worker(
            raw_root=self.root1,
            role="reviser",
            worker_slot="Robust_Reviser",
            task="Task to cancel",
            model_id="mimo-v2.6-flash",
            session_strategy="new",
        )
        sid = res["session_id"]
        job_id = res["job_id"]

        # Cancel job
        self.client.cancel_worker(job_id, self.root1)

        # Slot mapping remains intact!
        slot = self.client.store.get_slot(self.root1, "Robust_Reviser")
        self.assertIsNotNone(slot)
        self.assertEqual(slot["current_session_id"], sid)

        # And session lock was released cleanly!
        self.assertFalse(self.client.is_session_busy(sid)[0])

    # 11. 预测成本与实际成本能够正确关联和记录
    def test_11_prediction_and_actual_cost_feedback_loop(self):
        estimated = {"input_estimate": 600, "output_estimate": 300, "method": "heuristic"}
        res = self.client.start_worker(
            raw_root=self.root1,
            role="writer",
            worker_slot="Feedback_Slot",
            task="Write with prediction feedback",
            model_id="mimo-v2.6-flash",
            session_strategy="new",
            decision_reason="Initial task for research paper",
            estimated_cost=estimated,
            prediction_confidence=0.88,
        )
        job_id = res["job_id"]
        sid = res["session_id"]

        # Check initial decision recorded
        dec = self.client.store.get_decision_by_job(job_id)
        self.assertIsNotNone(dec)
        self.assertEqual(dec["selected_strategy"], "new")
        self.assertEqual(dec["task_outcome"], "submitted")
        self.assertEqual(dec["prediction_confidence"], 0.88)

        # Complete job with actual usage
        self.client.session_messages_map[sid] = [{
            "info": {"role": "assistant", "finish": "completed", "tokens": {"input": 620, "output": 290, "cache": {"read": 0, "write": 620}}, "cost": 0.003},
            "parts": [{"type": "text", "text": "Section completed"}],
        }]
        self.client.job_result(job_id, self.root1)

        # Check decision feedback record updated with actual usage
        updated_dec = self.client.store.get_decision_by_job(job_id)
        self.assertEqual(updated_dec["task_outcome"], "completed")
        self.assertIsNotNone(updated_dec["actual_usage"])
        actual_json = json.loads(updated_dec["actual_usage"])
        self.assertEqual(actual_json["input"], 620)
        self.assertEqual(actual_json["output"], 290)


    # 12. Reuse must reject a model mismatch because the Session keeps its creation model.
    def test_12_reuse_rejects_model_mismatch(self):
        created = self.client.start_worker(
            raw_root=self.root1,
            role="writer",
            worker_slot="Pinned_Writer",
            task="Create the initial writer session",
            model_id="mimo-v2.6-flash",
            session_strategy="new",
        )
        sid = created["session_id"]
        self.client.session_messages_map[sid] = [{
            "info": {"role": "assistant", "finish": "completed", "tokens": {"input": 100, "output": 20}},
            "parts": [{"type": "text", "text": "done"}],
        }]
        self.client.job_result(created["job_id"], self.root1)

        prompt_count_before = sum(1 for _, path, _ in self.client.calls if path.endswith("/prompt"))
        with self.assertRaisesRegex(core.BridgeError, "Slot.*已绑定"):
            self.client.start_worker(
                raw_root=self.root1,
                role="writer",
                worker_slot="Pinned_Writer",
                task="Attempt to switch the reused session model",
                model_id="kimi-k3.1-pro",
                session_strategy="reuse",
            )
        prompt_count_after = sum(1 for _, path, _ in self.client.calls if path.endswith("/prompt"))
        self.assertEqual(prompt_count_after, prompt_count_before)

    # 13. Fork must reject a Role mismatch because it inherits parent Agent/permissions.
    def test_13_fork_rejects_role_mismatch(self):
        created = self.client.start_worker(
            raw_root=self.root1,
            role="writer",
            worker_slot="Pinned_Fork",
            task="Create the parent writer session",
            model_id="mimo-v2.6-flash",
            session_strategy="new",
        )
        sid = created["session_id"]
        self.client.session_messages_map[sid] = [{
            "info": {"role": "assistant", "finish": "completed", "tokens": {"input": 100, "output": 20}},
            "parts": [{"type": "text", "text": "done"}],
        }]
        self.client.job_result(created["job_id"], self.root1)

        fork_count_before = sum(1 for _, path, _ in self.client.calls if "/fork" in path)
        with self.assertRaisesRegex(core.BridgeError, "Slot.*已绑定"):
            self.client.start_worker(
                raw_root=self.root1,
                role="reviewer",
                worker_slot="Pinned_Fork",
                task="Attempt to fork into a read-only reviewer session",
                model_id="mimo-v2.6-flash",
                session_strategy="fork",
            )
        fork_count_after = sum(1 for _, path, _ in self.client.calls if "/fork" in path)
        self.assertEqual(fork_count_after, fork_count_before)

    # 14. MCP instructions must describe on-demand Inspect and the slot binding invariant.
    def test_14_mcp_instructions_use_on_demand_inspect_and_slot_binding(self):
        self.assertIn("on-demand", mcp.SERVER_INSTRUCTIONS.lower())
        self.assertIn("not by a hard code-level lock", mcp.SERVER_INSTRUCTIONS.lower())
        self.assertIn("not a mandatory preflight", mcp.SERVER_INSTRUCTIONS.lower())
        self.assertIn("exact Role/Model", mcp.SERVER_INSTRUCTIONS)
        inspect_tool = next(t for t in mcp.TOOLS if t["name"] == "opencode_go_inspect")
        start_tool = next(t for t in mcp.TOOLS if t["name"] == "opencode_go_start")
        self.assertNotIn("每次新任务开始前先调用", inspect_tool["description"])
        self.assertNotIn("每次调用前必须已 inspect", start_tool["description"])

    # 15. Cross-slot Fork must validate the source Session's persisted Model binding.
    def test_15_cross_slot_fork_rejects_parent_model_mismatch(self):
        created = self.client.start_worker(
            raw_root=self.root1,
            role="writer",
            worker_slot="CrossSlot_Parent",
            task="Create parent writer session",
            model_id="mimo-v2.6-flash",
            session_strategy="new",
        )
        sid = created["session_id"]
        self.client.session_messages_map[sid] = [{
            "info": {"role": "assistant", "finish": "completed", "tokens": {"input": 100, "output": 20}},
            "parts": [{"type": "text", "text": "done"}],
        }]
        self.client.job_result(created["job_id"], self.root1)

        fork_count_before = sum(1 for _, path, _ in self.client.calls if "/fork" in path)
        with self.assertRaisesRegex(core.BridgeError, "父 Session 已绑定"):
            self.client.start_worker(
                raw_root=self.root1,
                role="writer",
                worker_slot="CrossSlot_Invalid_Branch",
                session_id=sid,
                task="Attempt cross-slot fork with a different model",
                model_id="kimi-k3.1-pro",
                session_strategy="fork",
            )
        fork_count_after = sum(1 for _, path, _ in self.client.calls if "/fork" in path)
        self.assertEqual(fork_count_after, fork_count_before)


    # 16. An explicit parent Session from another Project must never be forked here.
    def test_16_cross_project_fork_rejects_foreign_parent(self):
        created = self.client.start_worker(
            raw_root=self.root1,
            role="writer",
            worker_slot="Foreign_Project_Parent",
            task="Create parent session in project one",
            model_id="mimo-v2.6-flash",
            session_strategy="new",
        )
        sid = created["session_id"]
        self.client.session_messages_map[sid] = [{
            "info": {"role": "assistant", "finish": "completed", "tokens": {"input": 100, "output": 20}},
            "parts": [{"type": "text", "text": "done"}],
        }]
        self.client.job_result(created["job_id"], self.root1)

        fork_count_before = sum(1 for _, path, _ in self.client.calls if "/fork" in path)
        with self.assertRaisesRegex(core.BridgeError, "没有可验证的 Session 历史记录"):
            self.client.start_worker(
                raw_root=self.root2,
                role="writer",
                worker_slot="Foreign_Project_Branch",
                session_id=sid,
                task="Attempt to fork a session from another Project",
                model_id="mimo-v2.6-flash",
                session_strategy="fork",
            )
        fork_count_after = sum(1 for _, path, _ in self.client.calls if "/fork" in path)
        self.assertEqual(fork_count_after, fork_count_before)



    # 17. Fork must reject a parent Session that still has an active Job.
    def test_17_fork_rejects_busy_parent_session(self):
        created = self.client.start_worker(
            raw_root=self.root1,
            role="writer",
            worker_slot="Busy_Parent",
            task="Keep the parent session active",
            model_id="mimo-v2.6-flash",
            session_strategy="new",
        )
        sid = created["session_id"]
        fork_count_before = sum(1 for _, path, _ in self.client.calls if "/fork" in path)

        with self.assertRaisesRegex(core.BridgeError, "父 Session 正被 Job"):
            self.client.start_worker(
                raw_root=self.root1,
                role="writer",
                worker_slot="Busy_Parent_Branch",
                session_id=sid,
                task="Attempt to fork while the parent is still busy",
                model_id="mimo-v2.6-flash",
                session_strategy="fork",
            )

        fork_count_after = sum(1 for _, path, _ in self.client.calls if "/fork" in path)
        self.assertEqual(fork_count_after, fork_count_before)

if __name__ == "__main__":
    unittest.main(verbosity=2)
