from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

MCP_SERVER_ID = "opencode-go-worker-manager"
MCP_CALL_TOOL = "call_mcp_tool"

WORKER_TOOLS = {
    "opencode_go_inspect",
    "opencode_go_start",
    "opencode_go_status",
    "opencode_go_wait_result",
    "opencode_go_result",
    "opencode_go_list_jobs",
    "opencode_go_permissions",
    "opencode_go_permission_reply",
    "opencode_go_cancel",
    "opencode_go_session_inspect",
    "opencode_go_session_predict_cost",
    "opencode_go_session_update_summary",
    "opencode_go_predict_session_cost",
    "opencode_go_update_session_summary",
    "opencode_go_benchmark_manifest",
}


def _deny(reason: str) -> dict[str, Any]:
    return {"decision": "deny", "reason": reason}


def _canonical_directory(raw: Any) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("当前 Antigravity 工作区路径为空或无效。")

    candidate = Path(os.path.expandvars(os.path.expanduser(raw.strip())))

    if not candidate.is_absolute():
        raise ValueError("Antigravity 提供的工作区路径不是绝对路径。")

    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ValueError(
            f"工作区路径不存在或无法解析：{raw!r}；{exc}"
        ) from exc

    if not resolved.is_dir():
        raise ValueError(f"工作区路径不是目录：{str(resolved)}")

    return str(resolved)


def _resolve_single_workspace(workspace_paths: Any) -> str:
    if not isinstance(workspace_paths, list) or not workspace_paths:
        raise ValueError(
            "Antigravity 没有提供 workspacePaths；拒绝猜测 Project 路径。"
        )

    unique: dict[str, str] = {}

    for raw in workspace_paths:
        canonical = _canonical_directory(raw)
        key = os.path.normcase(os.path.normpath(canonical))
        unique[key] = canonical

    if len(unique) != 1:
        raise ValueError(
            "当前会话包含多个不同的工作区根目录；"
            "为避免跨项目操作，拒绝自动选择。"
        )

    return next(iter(unique.values()))


def _select_worker_arguments(
    tool_name: Any,
    tool_args: Any,
) -> tuple[str, dict[str, Any]] | None:

    # Actual Antigravity MCP wrapper observed in the user's Hook payload:
    # toolCall.name = call_mcp_tool
    # toolCall.args.ServerName
    # toolCall.args.ToolName
    # toolCall.args.Arguments
    if tool_name == MCP_CALL_TOOL:
        if not isinstance(tool_args, dict):
            raise ValueError("call_mcp_tool 参数结构无效。")

        server_name = tool_args.get("ServerName")
        actual_tool = tool_args.get("ToolName")

        # Do not interfere with other MCP servers.
        if server_name != MCP_SERVER_ID:
            return None

        # Only route the eight declared Worker Manager tools.
        if actual_tool not in WORKER_TOOLS:
            return None

        arguments = tool_args.get("Arguments")
        if not isinstance(arguments, dict):
            raise ValueError("Worker MCP 调用缺少有效的 Arguments 对象。")

        return "Arguments", arguments

    # Retain direct-name support for isolated unit/protocol tests.
    if tool_name in WORKER_TOOLS:
        if not isinstance(tool_args, dict):
            raise ValueError("Worker 工具参数不是对象。")
        return "direct", tool_args

    return None


def handle_payload(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return _deny("Hook 输入不是有效 JSON 对象。")

    tool_call = payload.get("toolCall")
    if not isinstance(tool_call, dict):
        return _deny("Hook 输入缺少 toolCall。")

    try:
        selected = _select_worker_arguments(
            tool_call.get("name"),
            tool_call.get("args"),
        )

        # Other MCP servers and other tools remain unaffected.
        if selected is None:
            return {"decision": "allow"}

        argument_key, original_arguments = selected
        root = _resolve_single_workspace(payload.get("workspacePaths"))

        # Antigravity overwrite is a shallow merge at toolCall.args.
        # Replace the complete nested Arguments object so its project_root
        # is changed while all other original arguments are preserved.
        updated_arguments = dict(original_arguments)
        updated_arguments["project_root"] = root

        if argument_key == "Arguments":
            return {
                "decision": "allow",
                "overwrite": {
                    "Arguments": updated_arguments
                },
            }

        return {
            "decision": "allow",
            "overwrite": {
                "project_root": root
            },
        }

    except ValueError as exc:
        return _deny(str(exc))


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read())
        result = handle_payload(payload)
    except Exception as exc:
        result = _deny(
            "Worker Project 路由发生错误；为安全起见已拒绝调用："
            f"{type(exc).__name__}: {exc}"
        )

    sys.stdout.write(
        json.dumps(result, ensure_ascii=False, separators=(",", ":")) + "\n"
    )
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
