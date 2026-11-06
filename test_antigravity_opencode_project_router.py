from __future__ import annotations

import unittest
from pathlib import Path

from antigravity_opencode_project_router import handle_payload

SERVER = "opencode-go-worker-manager"
ROOT = Path(__file__).resolve().parent


class ProjectRouterTests(unittest.TestCase):
    def payload(
        self,
        tool="opencode_go_inspect",
        workspace_paths=None,
        server=SERVER,
        arguments=None,
        wrapper="call_mcp_tool",
    ):
        if workspace_paths is None:
            workspace_paths = [str(ROOT)]

        if arguments is None:
            arguments = {
                "project_root": "ROUTER_TEST_MUST_BE_REPLACED",
                "custom_option": "preserve-me",
            }

        if wrapper == "call_mcp_tool":
            args = {
                "ServerName": server,
                "ToolName": tool,
                "Arguments": arguments,
            }
        else:
            args = arguments

        return {
            "toolCall": {
                "name": wrapper,
                "args": args,
            },
            "workspacePaths": workspace_paths,
            "conversationId": "offline-test",
        }

    def test_actual_call_mcp_tool_nested_arguments_are_overwritten(self):
        result = handle_payload(self.payload())
        self.assertEqual(result["decision"], "allow")
        updated = result["overwrite"]["Arguments"]
        self.assertEqual(updated["project_root"], str(ROOT.resolve()))
        self.assertEqual(updated["custom_option"], "preserve-me")

    def test_actual_tool_server_and_name_are_recognized(self):
        result = handle_payload(self.payload(tool="opencode_go_start"))
        self.assertIn("Arguments", result["overwrite"])

    def test_all_eight_worker_tools_are_routed(self):
        names = [
            "opencode_go_inspect",
            "opencode_go_start",
            "opencode_go_status",
            "opencode_go_result",
            "opencode_go_list_jobs",
            "opencode_go_permissions",
            "opencode_go_permission_reply",
            "opencode_go_cancel",
        ]
        for name in names:
            with self.subTest(name=name):
                result = handle_payload(self.payload(tool=name))
                self.assertEqual(result["decision"], "allow")
                self.assertIn("Arguments", result["overwrite"])

    def test_other_mcp_server_is_unaffected(self):
        result = handle_payload(
            self.payload(server="another-mcp-server")
        )
        self.assertEqual(result, {"decision": "allow"})

    def test_unknown_tool_on_same_server_is_unaffected(self):
        result = handle_payload(self.payload(tool="some_other_tool"))
        self.assertEqual(result, {"decision": "allow"})

    def test_missing_arguments_object_is_denied(self):
        result = handle_payload(
            self.payload(arguments=None) | {
                "toolCall": {
                    "name": "call_mcp_tool",
                    "args": {
                        "ServerName": SERVER,
                        "ToolName": "opencode_go_inspect",
                        "Arguments": None,
                    },
                }
            }
        )
        self.assertEqual(result["decision"], "deny")

    def test_duplicate_workspace_paths_are_deduplicated(self):
        result = handle_payload(
            self.payload(workspace_paths=[str(ROOT), str(ROOT)])
        )
        self.assertEqual(result["decision"], "allow")

    def test_multiple_distinct_workspaces_are_denied(self):
        result = handle_payload(
            self.payload(workspace_paths=[str(ROOT), str(ROOT.parent)])
        )
        self.assertEqual(result["decision"], "deny")

    def test_missing_workspace_paths_are_denied(self):
        result = handle_payload(
            self.payload() | {"workspacePaths": None}
        )
        self.assertEqual(result["decision"], "deny")

    def test_empty_workspace_paths_are_denied(self):
        result = handle_payload(self.payload(workspace_paths=[]))
        self.assertEqual(result["decision"], "deny")

    def test_nonexistent_workspace_is_denied(self):
        missing = ROOT / "__router_missing_test_directory__"
        result = handle_payload(self.payload(workspace_paths=[str(missing)]))
        self.assertEqual(result["decision"], "deny")

    def test_relative_workspace_path_is_denied(self):
        result = handle_payload(
            self.payload(workspace_paths=["relative/project"])
        )
        self.assertEqual(result["decision"], "deny")

    def test_file_path_is_denied_as_workspace(self):
        result = handle_payload(
            self.payload(workspace_paths=[str(Path(__file__))])
        )
        self.assertEqual(result["decision"], "deny")

    def test_direct_raw_tool_name_is_supported_for_tests(self):
        result = handle_payload(
            self.payload(wrapper="opencode_go_inspect")
        )
        self.assertEqual(result["decision"], "allow")
        self.assertIn("project_root", result["overwrite"])

    def test_missing_tool_call_is_denied(self):
        result = handle_payload({"workspacePaths": [str(ROOT)]})
        self.assertEqual(result["decision"], "deny")


if __name__ == "__main__":
    unittest.main(verbosity=2)
