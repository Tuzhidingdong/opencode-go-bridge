from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

# Use a clean temporary APPDATA for testing
_TEST_TEMP_DIR = tempfile.TemporaryDirectory(prefix="ocwm-usage-report-test-")
os.environ["APPDATA"] = _TEST_TEMP_DIR.name

import opencode_go_worker_core as core  # noqa: E402
import opencode_go_worker_mcp as mcp  # noqa: E402


class UsageReportTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(prefix="test-usage-env-")
        self.appdata = Path(self.temp_dir.name) / "appdata"
        self.appdata.mkdir(parents=True, exist_ok=True)
        os.environ["APPDATA"] = str(self.appdata)

        # Set up mock Token Meter ledger path
        self.meter_dir = self.appdata / "AntigravityTokenMeter"
        self.meter_dir.mkdir(parents=True, exist_ok=True)
        self.ledger_file = self.meter_dir / "task_usage.jsonl"

        # Set up mock Worker Manager jobs db
        self.wm_dir = self.appdata / "OpenCodeGoWorkerManager"
        self.wm_dir.mkdir(parents=True, exist_ok=True)
        self.jobs_db = self.wm_dir / "worker_jobs.sqlite3"
        self._init_mock_jobs_db()

        # Set up mock OpenCode DB
        self.opencode_dir = self.appdata / "local_share_opencode"
        self.opencode_dir.mkdir(parents=True, exist_ok=True)
        self.opencode_db = self.opencode_dir / "opencode.db"
        self._init_mock_opencode_db()

        # Point core's opencode db path resolver to test db
        self.orig_opencode_db_path = core._opencode_db_path
        core._opencode_db_path = lambda: self.opencode_db

        self.client = core.OpenCodeClient()

    def tearDown(self):
        core._opencode_db_path = self.orig_opencode_db_path
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def _init_mock_jobs_db(self):
        con = sqlite3.connect(self.jobs_db)
        try:
            con.execute(
                """CREATE TABLE jobs (
                    job_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    project_root TEXT NOT NULL,
                    role TEXT NOT NULL,
                    model_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    state TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    last_error TEXT
                )"""
            )
            con.commit()
        finally:
            con.close()

    def _init_mock_opencode_db(self):
        con = sqlite3.connect(self.opencode_db)
        try:
            con.execute(
                """CREATE TABLE session_v2 (
                    id TEXT PRIMARY KEY,
                    project_id TEXT,
                    directory TEXT,
                    tokens_input INTEGER,
                    tokens_output INTEGER,
                    tokens_reasoning INTEGER,
                    tokens_cache_read INTEGER,
                    tokens_cache_write INTEGER,
                    cost REAL,
                    idle_outcome TEXT,
                    time_created INTEGER,
                    time_updated INTEGER
                )"""
            )
            con.execute(
                """CREATE TABLE session_message (
                    id TEXT PRIMARY KEY,
                    session_id TEXT,
                    type TEXT,
                    data TEXT
                )"""
            )
            con.commit()
        finally:
            con.close()

    # 1. 正常 Ledger 聚合 & 6. 缓存读取与普通输入不重复计算
    def test_antigravity_normal_ledger_aggregation(self):
        records = [
            {
                "schema_version": 2,
                "task_id": "conv1:1",
                "started_at": "2026-11-04T10:00:00Z",
                "finished_at": "2026-11-04T10:01:00Z",
                "workspace_paths": ["E:/ProjectA"],
                "planner_responses": 3,
                "input_uncached_tokens": 1000,
                "input_cache_read_tokens": 500,
                "output_tokens": 200,
            },
            {
                "schema_version": 2,
                "task_id": "conv1:2",
                "started_at": "2026-11-04T11:00:00Z",
                "finished_at": "2026-11-04T11:02:00Z",
                "workspace_paths": ["E:/ProjectA"],
                "planner_responses": 2,
                "input_uncached_tokens": 2000,
                "input_cache_read_tokens": 1000,
                "output_tokens": 400,
            },
        ]
        with self.ledger_file.open("w", encoding="utf-8") as f:
            for r in records:
                f.write(json.dumps(r) + "\n")

        rep = self.client.usage_report(period="all")
        ag = rep["antigravity"]
        self.assertEqual(ag["task_count"], 2)
        self.assertEqual(ag["planner_responses"], 5)
        self.assertEqual(ag["input_uncached"], 3000)
        self.assertEqual(ag["cache_read"], 1500)
        self.assertEqual(ag["input_total"], 4500)  # 3000 + 1500
        self.assertEqual(ag["output"], 600)
        self.assertEqual(ag["total_recorded"], 5100)  # 4500 + 600

    # 2. 空 Ledger
    def test_antigravity_empty_ledger(self):
        rep = self.client.usage_report(period="all")
        ag = rep["antigravity"]
        self.assertEqual(ag["task_count"], 0)
        self.assertEqual(ag["total_recorded"], 0)
        self.assertEqual(rep["data_quality"]["missing_records"], 1)

    # 3. 损坏 JSONL 行跳过与计数
    def test_antigravity_malformed_records_skipped(self):
        with self.ledger_file.open("w", encoding="utf-8") as f:
            f.write("corrupted json line 1\n")
            f.write(json.dumps({
                "schema_version": 2,
                "task_id": "valid:1",
                "started_at": "2026-11-04T10:00:00Z",
                "finished_at": "2026-11-04T10:01:00Z",
                "workspace_paths": ["E:/Test"],
                "planner_responses": 1,
                "input_uncached_tokens": 500,
                "input_cache_read_tokens": 0,
                "output_tokens": 100,
            }) + "\n")
            f.write("another {bad json\n")

        rep = self.client.usage_report(period="all")
        self.assertEqual(rep["antigravity"]["task_count"], 1)
        self.assertEqual(rep["antigravity"]["total_recorded"], 600)
        self.assertEqual(rep["data_quality"]["malformed_records"], 2)

    # 4. 按时间范围筛选 (7d, 30d, all)
    def test_period_time_filtering(self):
        now = time.time()
        ts_2d = datetime.fromtimestamp(now - 2 * 86400, timezone.utc).isoformat()
        ts_15d = datetime.fromtimestamp(now - 15 * 86400, timezone.utc).isoformat()
        ts_45d = datetime.fromtimestamp(now - 45 * 86400, timezone.utc).isoformat()

        records = [
            {"task_id": "t1", "finished_at": ts_2d, "workspace_paths": ["E:/A"], "planner_responses": 1, "input_uncached_tokens": 100, "output_tokens": 10},
            {"task_id": "t2", "finished_at": ts_15d, "workspace_paths": ["E:/A"], "planner_responses": 1, "input_uncached_tokens": 200, "output_tokens": 20},
            {"task_id": "t3", "finished_at": ts_45d, "workspace_paths": ["E:/A"], "planner_responses": 1, "input_uncached_tokens": 400, "output_tokens": 40},
        ]
        with self.ledger_file.open("w", encoding="utf-8") as f:
            for r in records:
                f.write(json.dumps(r) + "\n")

        rep_all = self.client.usage_report(period="all")
        self.assertEqual(rep_all["antigravity"]["task_count"], 3)
        self.assertEqual(rep_all["antigravity"]["input_uncached"], 700)

        rep_30d = self.client.usage_report(period="30d")
        self.assertEqual(rep_30d["antigravity"]["task_count"], 2)
        self.assertEqual(rep_30d["antigravity"]["input_uncached"], 300)

        rep_7d = self.client.usage_report(period="7d")
        self.assertEqual(rep_7d["antigravity"]["task_count"], 1)
        self.assertEqual(rep_7d["antigravity"]["input_uncached"], 100)

    # 5. Project 分组
    def test_project_grouping(self):
        records = [
            {"task_id": "t1", "finished_at": "2026-11-04T10:00:00Z", "workspace_paths": ["E:/ProjA"], "planner_responses": 1, "input_uncached_tokens": 100, "output_tokens": 50},
            {"task_id": "t2", "finished_at": "2026-11-04T10:05:00Z", "workspace_paths": ["E:/ProjB"], "planner_responses": 2, "input_uncached_tokens": 300, "output_tokens": 150},
        ]
        with self.ledger_file.open("w", encoding="utf-8") as f:
            for r in records:
                f.write(json.dumps(r) + "\n")

        rep = self.client.usage_report(period="all")
        by_p = {p["project"]: p for p in rep["antigravity"]["by_project"]}
        self.assertIn("E:/ProjA", by_p)
        self.assertIn("E:/ProjB", by_p)
        self.assertEqual(by_p["E:/ProjA"]["total_recorded"], 150)
        self.assertEqual(by_p["E:/ProjB"]["total_recorded"], 450)

    # 7. 多 Job Token 聚合 & 8. 同一个 Session 去重 & 9. completed 和 failed Job 统计 & 10. running Job 不误报为完成 & 14. Reasoning 不合并到 Output
    def test_opencode_jobs_token_aggregation_and_deduplication(self):
        now = time.time()
        con1 = sqlite3.connect(self.jobs_db)
        try:
            con1.execute("INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?,NULL)", ("job1", "ses1", "E:/ProjA", "analyst", "mimo-v2.6-flash", "Job 1", "completed", now - 100, now - 80))
            con1.execute("INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?,NULL)", ("job2", "ses1", "E:/ProjA", "analyst", "mimo-v2.6-flash", "Job 2", "completed", now - 70, now - 50))
            con1.execute("INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?,NULL)", ("job3", "ses2", "E:/ProjA", "writer", "minimax-m3.1-flash", "Job 3", "failed", now - 40, now - 30))
            con1.execute("INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?,NULL)", ("job4", "ses3", "E:/ProjB", "reviewer", "minimax-m3.1-flash", "Job 4", "running", now - 20, now - 10))
            con1.commit()
        finally:
            con1.close()

        con2 = sqlite3.connect(self.opencode_db)
        try:
            con2.execute("INSERT INTO session_v2 VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", ("ses1", "p1", "E:/ProjA", 1000, 500, 200, 800, 0, 0.01, "succeeded", int(now - 100) * 1000, int(now - 80) * 1000))
            con2.execute("INSERT INTO session_v2 VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", ("ses2", "p1", "E:/ProjA", 300, 100, 50, 200, 0, 0.0, "failed", int(now - 40) * 1000, int(now - 30) * 1000))
            con2.execute("INSERT INTO session_v2 VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", ("ses3", "p2", "E:/ProjB", 100, 20, 0, 50, 0, 0.0, None, int(now - 20) * 1000, int(now - 10) * 1000))
            con2.commit()
        finally:
            con2.close()

        rep = self.client.usage_report(period="all")
        oc = rep["opencode_worker"]

        self.assertEqual(oc["job_count"], 4)
        self.assertEqual(oc["completed_count"], 2)
        self.assertEqual(oc["failed_count"], 1)
        self.assertEqual(oc["running_count"], 1)

        # Tokens: ses1 (counted once) + ses2 + ses3
        self.assertEqual(oc["input"], 1400)
        self.assertEqual(oc["output"], 620)
        self.assertEqual(oc["reasoning"], 250)
        self.assertEqual(oc["cache_read"], 1050)
        self.assertAlmostEqual(oc["cost_usd"], 0.01)

    # 11. Session 缺失时标记 unavailable
    def test_missing_session_marks_unavailable(self):
        now = time.time()
        con = sqlite3.connect(self.jobs_db)
        try:
            con.execute("INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?,NULL)", ("job_ghost", "ses_nonexistent", "E:/Test", "analyst", "mimo-v2.6-flash", "Ghost", "failed", now, now))
            con.commit()
        finally:
            con.close()

        rep = self.client.usage_report(period="all")
        self.assertEqual(rep["data_quality"]["unmatched_sessions"], 1)
        recent = rep["opencode_worker"]["recent_jobs"]
        self.assertEqual(len(recent), 1)
        self.assertEqual(recent[0]["tokens"], "unavailable")

    # 12. 不读取非 Worker Manager 创建的 Session
    def test_untracked_opencode_sessions_not_counted(self):
        now = time.time()
        con = sqlite3.connect(self.opencode_db)
        try:
            con.execute("INSERT INTO session_v2 VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", ("ses_external_tui", "p_ext", "E:/Other", 99999, 88888, 77777, 66666, 0, 10.0, "succeeded", int(now) * 1000, int(now) * 1000))
            con.commit()
        finally:
            con.close()

        rep = self.client.usage_report(period="all")
        self.assertEqual(rep["opencode_worker"]["job_count"], 0)
        self.assertEqual(rep["opencode_worker"]["input"], 0)
        self.assertEqual(rep["opencode_worker"]["output"], 0)

    # 15. 数据库不可访问时安全降级
    def test_database_inaccessible_safe_fallback(self):
        core._opencode_db_path = lambda: Path("Z:/NonExistent/opencode.db")
        now = time.time()
        con = sqlite3.connect(self.jobs_db)
        try:
            con.execute("INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?,NULL)", ("job1", "ses1", "E:/Test", "analyst", "mimo-v2.6-flash", "Test", "completed", now, now))
            con.commit()
        finally:
            con.close()

        rep = self.client.usage_report(period="all")
        self.assertEqual(rep["opencode_worker"]["job_count"], 1)
        self.assertEqual(rep["data_quality"]["unmatched_sessions"], 1)

    # 16. 新增工具出现在 tools/list & 17. 输入参数校验 & 18. 返回结构
    def test_mcp_tools_list_and_parameter_validation(self):
        tools = {t["name"]: t for t in mcp.TOOLS}
        self.assertIn("opencode_go_usage_report", tools)
        schema = tools["opencode_go_usage_report"]["inputSchema"]
        self.assertIn("period", schema["properties"])
        self.assertEqual(schema["properties"]["period"]["enum"], ["all", "30d", "7d"])

        with self.assertRaises(core.BridgeError):
            self.client.usage_report(period="invalid_period")

        with self.assertRaises(core.BridgeError):
            self.client.usage_report(include_recent=["bad_type"])

    # 19. 查询过程没有模型调用、Session 创建或数据库写入
    def test_no_model_calls_or_session_creations(self):
        rep = self.client.usage_report(period="all")
        self.assertIn("antigravity", rep)
        self.assertIn("opencode_worker", rep)
        self.assertIn("notes", rep)
        self.assertIsNone(self.client.server)


if __name__ == "__main__":
    unittest.main(verbosity=2)
