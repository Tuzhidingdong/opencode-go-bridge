from __future__ import annotations

import argparse
import json
import sys
import traceback

from opencode_go_worker_core import CLIENT, BridgeError, shutdown


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Read-only local OpenCode Go API probe. No worker session is created and no model is called."
    )
    parser.add_argument("project_root", help="Exact absolute root of the currently active Antigravity Project")
    args = parser.parse_args()

    try:
        result = CLIENT.models_and_usage(args.project_root)
        required_agents = result.get("required_agents_available", {})
        missing_agents = [agent for agent, available in required_agents.items() if not available]
        if missing_agents:
            raise BridgeError(
                "当前 OpenCode Project 缺少 Worker Manager 所需 Agent："
                + ", ".join(missing_agents)
                + "。未创建 Session，也未调用模型。"
            )

        active_raw = CLIENT.request("GET", "/api/session/active")
        active_data = active_raw.get("data", active_raw) if isinstance(active_raw, dict) else active_raw
        if not isinstance(active_data, dict):
            raise BridgeError("OpenCode V2 session-active API 返回格式异常；暂不注册 MCP。")
        server_handle = CLIENT.server
        output = {
            "probe": "passed",
            "read_only": True,
            "model_calls": 0,
            "sessions_created": 0,
            "project_root": result["project_root"],
            "server": result["server"],
            "server_health_check": "GET /api/info returned a JSON object",
            "server_working_directory": result["project_root"],
            "server_local_url": server_handle.base_url if server_handle else None,
            "active_session_endpoint": "reachable",
            "active_session_count": len(active_data),
            "provider_filter": result["provider_filter"],
            "models_total": result["models_total"],
            "models_available": result["models_available"],
            "model_summaries": [
                {
                    "id": model.get("id"),
                    "name": model.get("name"),
                    "status": model.get("status"),
                    "context_limit": (model.get("limit") or {}).get("context"),
                    "output_limit": (model.get("limit") or {}).get("output"),
                    "input_modalities": (model.get("capabilities") or {}).get("input", []),
                    "variants": [variant.get("id") for variant in model.get("variants", []) if isinstance(variant, dict)],
                }
                for model in result["models"]
            ],
            "inactive_models": result["inactive_models"],
            "agents": result["agents"],
            "required_agents_available": required_agents,
            "usage": result["usage"],
            "notes": result["notes"],
            "worker_store": "created/checked under %APPDATA%\\OpenCodeGoWorkerManager; no Project files are written",
        }
        print(json.dumps(output, ensure_ascii=False, indent=2, default=str))
        return 0
    except Exception as exc:
        print(f"PROBE_FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        print("OpenCode server log: %APPDATA%\\OpenCodeGoWorkerManager\\opencode-server.log", file=sys.stderr)
        if not isinstance(exc, BridgeError):
            traceback.print_exc(file=sys.stderr)
        return 1
    finally:
        shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
