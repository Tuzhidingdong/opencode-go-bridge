from __future__ import annotations

import json
import os
import tempfile
import unittest
import uuid
from pathlib import Path

_TEST_ROOT = tempfile.TemporaryDirectory(prefix="ocwm-test-")
os.environ["APPDATA"] = _TEST_ROOT.name

import opencode_go_worker_core as core  # noqa: E402
import opencode_go_worker_mcp as mcp  # noqa: E402


class FakeClient(core.OpenCodeClient):
    def __init__(self):
        super().__init__()
        self.calls = []
        self.started_root = None

    def ensure_server(self, raw_project_root=None):
        if raw_project_root:
            self.started_root = core.canonical_project_root(raw_project_root)
        return None

    def request(self, method, path, body=None, timeout=core.API_TIMEOUT_SECONDS):
        self.calls.append((method, path, body))
        if path.startswith("/api/model?"):
            return {"data": [
                {"id": "mimo-v2.6-flash", "providerID": "opencode-go", "name": "MiMo Flash", "status": "active"},
                {"id": "kimi-k3.1-pro", "providerID": "opencode-go", "name": "Kimi Pro", "status": "active"},
                {"id": "big-pickle", "providerID": "opencode", "name": "Other Provider", "status": "active"},
                {"id": "old-model", "providerID": "opencode-go", "name": "Inactive", "status": "disabled"},
            ]}
        if path.startswith("/api/experimental/usage/limits?"):
            return {"data": {"workspace": {"name": "Personal"}, "go": {"weekly": {"percent": 0}}, "budgets": []}}
        if path == "/api/info":
            return {"data": {"ready": True, "version": "2.2.5"}}
        if path.startswith("/api/agent?"):
            return {"data": [
                {"id": "build", "name": "Build", "mode": "primary"},
                {"id": "explore", "name": "Explore", "mode": "subagent"},
            ]}
        if path == "/api/session/active":
            return {"data": {}}
        if path == "/api/session":
            return {"data": {"id": f"ses_{uuid.uuid4().hex[:12]}"}}
        if path.endswith("/prompt"):
            return None
        if path.endswith("/permission"):
            return {"data": []}
        if path.endswith("/message?limit=100&order=asc"):
            return {"data": []}
        if path.startswith("/api/session/ses_"):
            return {"data": {"title": "Test"}}
        raise AssertionError(f"Unexpected request: {method} {path}")


class WorkerManagerTests(unittest.TestCase):
    def setUp(self):
        self.client = FakeClient()
        self.temp = tempfile.TemporaryDirectory(prefix="ocwm-project-")
        self.root = str(Path(self.temp.name).resolve())

    def tearDown(self):
        self.temp.cleanup()

    def test_model_filter_excludes_other_providers_and_disabled(self):
        result = self.client.models_and_usage(self.root)
        self.assertEqual(result["models_total"], 3)
        self.assertEqual(result["models_available"], 2)
        self.assertEqual({m["id"] for m in result["models"]}, {"mimo-v2.6-flash", "kimi-k3.1-pro"})
        self.assertEqual(result["usage"]["workspace"]["name"], "Personal")
        self.assertEqual(self.client.started_root, self.root)

    def test_server_info_rejects_html_fallback(self):
        with self.assertRaises(core.BridgeError):
            core._server_info("<!doctype html><html><body>OpenCode</body></html>")
        self.assertEqual(core._server_info({"data": {"version": "2.2.5"}})["version"], "2.2.5")

    def test_inspect_discovers_required_agents(self):
        result = self.client.models_and_usage(self.root)
        self.assertTrue(result["required_agents_available"]["build"])
        self.assertTrue(result["required_agents_available"]["explore"])

    def test_worker_creates_independent_session_with_root_and_model(self):
        result = self.client.start_worker(
            raw_root=self.root,
            role="writer",
            task="Write a short draft based on project files.",
            model_id="opencode-go/mimo-v2.6-flash",
        )
        self.assertEqual(result["state"], "submitted")
        create_call = next(x for x in self.client.calls if x[1] == "/api/session")
        body = create_call[2]
        self.assertEqual(body["location"]["directory"], self.root)
        self.assertEqual(body["model"], {"providerID": "opencode-go", "id": "mimo-v2.6-flash"})
        self.assertTrue(any(x[1].endswith("/prompt") for x in self.client.calls))
        self.assertTrue(any(r["action"] == "external_directory" and r["effect"] == "deny" for r in body["permissions"]))
        self.assertTrue(any(r["action"] == "subagent" and r["effect"] == "deny" for r in body["permissions"]))
        self.assertTrue(any(r["action"] == "read" and r["resource"] == "../*" and r["effect"] == "deny" for r in body["permissions"]))
        self.assertTrue(any(r["action"] == "edit" and r["resource"] == "*/../*" and r["effect"] == "deny" for r in body["permissions"]))
        self.assertTrue(any(r["action"] == "shell" and r["effect"] == "ask" for r in body["permissions"]))
        self.assertFalse(any(r["action"] in ("bash", "task", "mcp") for r in body["permissions"]))

    def test_reviewer_is_read_only(self):
        rules = self.client._permissions_for_role("reviewer")
        self.assertTrue(any(r["action"] == "edit" and r["effect"] == "deny" for r in rules))
        self.assertTrue(any(r["action"] == "shell" and r["effect"] == "deny" for r in rules))
        self.assertTrue(any(r["action"] == "subagent" and r["effect"] == "deny" for r in rules))

    def test_worker_job_is_scoped_to_its_project_root(self):
        self.client.store.add({
            "job_id": "job_scope_test",
            "session_id": "ses_scope_test",
            "project_root": self.root,
            "role": "reviewer",
            "model_id": "mimo-v2.6-flash",
            "title": "Scope test",
        })
        loaded = self.client._load_job("job_scope_test", self.root)
        self.assertEqual(loaded["project_root"], self.root)
        other = tempfile.TemporaryDirectory(prefix="ocwm-other-project-")
        try:
            with self.assertRaises(core.BridgeError):
                self.client._load_job("job_scope_test", other.name)
        finally:
            other.cleanup()

    def test_writer_may_edit_but_shell_requires_approval(self):
        rules = self.client._permissions_for_role("writer")
        self.assertTrue(any(r["action"] == "edit" and r["effect"] == "allow" for r in rules))
        self.assertTrue(any(r["action"] == "shell" and r["effect"] == "ask" for r in rules))

    def test_batch_permission_reply_uses_one_manager_call_for_multiple_once_approvals(self):
        job_id = "job_permission_batch"
        session_id = "ses_permission_batch"
        self.client.store.add({
            "job_id": job_id,
            "session_id": session_id,
            "project_root": self.root,
            "role": "writer",
            "model_id": "mimo-v2.6-flash",
            "title": "Permission batch test",
        })
        result = self.client.reply_permission(
            job_id=job_id,
            raw_root=self.root,
            request_ids=["perm_a", "perm_b", "perm_c"],
            decision="once",
        )
        self.assertEqual(result["state"], "completed")
        self.assertEqual(result["replied_request_ids"], ["perm_a", "perm_b", "perm_c"])
        calls = [call for call in self.client.calls if "/permission/" in call[1] and call[1].endswith("/reply")]
        self.assertEqual(len(calls), 3)

    def test_batch_permission_reject_is_limited_to_one_request(self):
        with self.assertRaises(core.BridgeError):
            self.client.reply_permission(
                job_id="missing",
                raw_root=self.root,
                request_ids=["perm_a", "perm_b"],
                decision="reject",
            )

    def test_non_go_model_rejected(self):
        with self.assertRaises(core.BridgeError):
            self.client.start_worker(self.root, "writer", "do work", "big-pickle")

    def test_relative_or_missing_root_rejected(self):
        with self.assertRaises(core.BridgeError):
            core.canonical_project_root(".")
        with self.assertRaises(core.BridgeError):
            core.canonical_project_root(str(Path(self.root) / "not-found"))

    def test_mcp_exposes_worker_tools(self):
        names = {tool["name"] for tool in mcp.TOOLS}
        expected = {
            "opencode_go_inspect", "opencode_go_start", "opencode_go_status",
            "opencode_go_result", "opencode_go_list_jobs", "opencode_go_permissions",
            "opencode_go_permission_reply", "opencode_go_cancel",
        }
        self.assertTrue(expected.issubset(names))
        permission_tool = next(tool for tool in mcp.TOOLS if tool["name"] == "opencode_go_permission_reply")
        self.assertIn("request_ids", permission_tool["inputSchema"]["properties"])
        self.assertIn("oneOf", permission_tool["inputSchema"])
        for tool in mcp.TOOLS:
            if tool["name"] in {
                "opencode_go_status", "opencode_go_result", "opencode_go_list_jobs",
                "opencode_go_permissions", "opencode_go_permission_reply", "opencode_go_cancel",
            }:
                self.assertIn("project_root", tool["inputSchema"]["required"])

    def test_token_aggregation(self):
        messages = [
            {"info": {"role": "assistant", "tokens": {"input": 100, "output": 20, "reasoning": 4, "cache": {"read": 50, "write": 3}}, "cost": 0.001}, "parts": []},
            {"info": {"role": "user", "tokens": {"input": 500}}, "parts": []},
            {"info": {"role": "assistant", "tokens": {"input": 25, "output": 10, "reasoning": 2, "cache": {"read": 9, "write": 0}}, "cost": 0.002}, "parts": []},
        ]
        totals = core._token_totals(messages)
        self.assertEqual(totals["input"], 125)
        self.assertEqual(totals["output"], 30)
        self.assertEqual(totals["cache_read"], 59)
        self.assertAlmostEqual(totals["cost_usd"], 0.003)


class ErrorClassificationTests(unittest.TestCase):
    def test_explicit_key_expired_phrase_classification(self):
        # 1. 明确错误文本 This API key has expired -> AUTH_KEY_EXPIRED
        for phrase in ("This API key has expired", "API key expired", "API key has expired"):
            err = core.classify_error(status_code=401, raw_payload={"error": phrase})
            self.assertEqual(err.category, "AUTH_KEY_EXPIRED")
            self.assertIn("OpenCode Go API Key 已过期", err.user_message)

    def test_http_401_invalid_key_classification(self):
        # 2. HTTP 401 + invalid API key -> AUTH_FAILED
        err = core.classify_error(status_code=401, raw_payload={"error": "invalid API key"})
        self.assertEqual(err.category, "AUTH_FAILED")
        self.assertIn("OpenCode Go 认证失败", err.user_message)
        self.assertNotIn("已过期", err.category)

    def test_http_403_subscription_error_classification(self):
        # 3. HTTP 403 + subscription error -> AUTH_FAILED
        err = core.classify_error(status_code=403, raw_payload={"message": "subscription error: account suspended"})
        self.assertEqual(err.category, "AUTH_FAILED")
        self.assertIn("OpenCode Go 认证失败", err.user_message)

    def test_http_429_rate_limited(self):
        # 4. HTTP 429 -> RATE_LIMITED
        err = core.classify_error(status_code=429, raw_payload={"error": "Too Many Requests"})
        self.assertEqual(err.category, "RATE_LIMITED")
        self.assertIn("达到额度或请求频率限制", err.user_message)

    def test_http_500_server_error(self):
        # 5. HTTP 500 -> SERVER_ERROR
        err = core.classify_error(status_code=500, raw_payload={"error": "Internal Server Error"})
        self.assertEqual(err.category, "SERVER_ERROR")
        self.assertIn("服务端故障", err.user_message)

    def test_network_error(self):
        # 6. 网络连接失败 -> NETWORK_ERROR
        exc = core.urllib.error.URLError("Connection refused")
        err = core.classify_error(exc=exc)
        self.assertEqual(err.category, "NETWORK_ERROR")
        self.assertIn("网络连接失败", err.user_message)

    def test_nested_json_error_extraction(self):
        # 7. 嵌套 JSON 错误信息能够正确提取
        nested = {
            "response": {
                "errors": [
                    {"code": 401, "detail": {"message": "This API key has expired"}}
                ]
            }
        }
        extracted = core.extract_error_text(nested)
        self.assertIn("This API key has expired", extracted)
        err = core.classify_error(raw_payload=nested)
        self.assertEqual(err.category, "AUTH_KEY_EXPIRED")

    def test_model_invocation_auth_failure_preserves_job_and_reports_status(self):
        # 8 & 9. 模型调用阶段发生认证失败，Job 最终状态正确，status/result 返回明确分类
        temp = tempfile.TemporaryDirectory(prefix="ocwm-err-test-")
        root = str(Path(temp.name).resolve())
        client = FakeClient()

        orig_req = client.request

        def fake_request(method, path, body=None, timeout=core.API_TIMEOUT_SECONDS):
            if path.endswith("/prompt"):
                raise core.BridgeError(
                    "[AUTH_KEY_EXPIRED] " + core.AUTH_KEY_EXPIRED_MESSAGE,
                    category="AUTH_KEY_EXPIRED",
                    user_message=core.AUTH_KEY_EXPIRED_MESSAGE,
                    status_code=401,
                )
            return orig_req(method, path, body, timeout)

        client.request = fake_request
        try:
            with self.assertRaises(core.BridgeError) as ctx:
                client.start_worker(root, "writer", "test task", "mimo-v2.6-flash")
            self.assertEqual(ctx.exception.category, "AUTH_KEY_EXPIRED")

            jobs = client.store.list(root)
            self.assertEqual(len(jobs), 1)
            job = jobs[0]
            self.assertEqual(job["state"], "failed")
            self.assertTrue(job["session_id"].startswith("ses_"))

            client.request = orig_req
            status = client.job_status(job["job_id"], root)
            self.assertEqual(status["state"], "failed")
            self.assertEqual(status["error_category"], "AUTH_KEY_EXPIRED")
            self.assertIn("OpenCode Go API Key 已过期", status["error_message"])

            result = client.job_result(job["job_id"], root)
            self.assertEqual(result["state"], "failed")
            self.assertEqual(result["error_category"], "AUTH_KEY_EXPIRED")
            self.assertIn("OpenCode Go API Key 已过期", result["error_message"])
        finally:
            temp.cleanup()

    def test_mcp_dispatch_formats_structured_error(self):
        # Verify MCP tools/call returns structured error with error_category
        exc = core.BridgeError(
            "Expired key",
            category="AUTH_KEY_EXPIRED",
            user_message=core.AUTH_KEY_EXPIRED_MESSAGE,
            status_code=401,
        )
        orig = mcp.handle_tool
        mcp.handle_tool = lambda name, args: (_ for _ in ()).throw(exc)
        sent = []
        orig_send = mcp.send
        mcp.send = lambda msg: sent.append(msg)
        try:
            mcp.dispatch({"jsonrpc": "2.0", "id": 100, "method": "tools/call", "params": {"name": "opencode_go_start", "arguments": {}}})
            self.assertEqual(len(sent), 1)
            resp = sent[0]
            self.assertTrue(resp["result"]["isError"])
            structured = resp["result"]["structuredContent"]
            self.assertEqual(structured["error_category"], "AUTH_KEY_EXPIRED")
            self.assertEqual(structured["user_message"], core.AUTH_KEY_EXPIRED_MESSAGE)
        finally:
            mcp.handle_tool = orig
            mcp.send = orig_send


if __name__ == "__main__":
    unittest.main(verbosity=2)
