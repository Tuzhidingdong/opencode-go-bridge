from __future__ import annotations

import json
import sys
import traceback
from typing import Any

import os
import urllib.error
from datetime import datetime, timezone
from opencode_go_worker_core import CLIENT, BridgeError, shutdown, ErrorCategory, _compute_file_sha256, VERSION as CORE_VERSION

SERVER_NAME = "opencode-go-worker-manager"
SERVER_VERSION = "0.2.0"
MCP_START_TIME = datetime.now(timezone.utc).isoformat()
MCP_BUILD_HASH = _compute_file_sha256(__file__)[:12] if os.path.exists(__file__) else "dev" 
PROTOCOL_VERSION = "2025-03-26"

SERVER_INSTRUCTIONS = """# OpenCode Go Worker Manager Instructions & Supervisor Rules

You expose OpenCode Go as execution Workers under Antigravity supervision.
Always pass the exact active Antigravity Project root as `project_root`; never guess or hardcode it. Workers must stay inside that root. External-directory access, path traversal, and nested subagents are denied.
Default hierarchy is Supervisor -> one Worker for each task. Default concurrency is 3; hard maximum is 6. Do not create multi-worker chains or parallel workers unless the user explicitly requests them.

## 1. Task routing
- Handle conversation, planning, simple questions, small edits, and lightweight read-only checks directly in Antigravity.
- Delegate complex multi-file implementation, substantial codebase analysis, bulk document/data work, or genuinely independent read-only audits.
- Do not launch a Worker merely to demonstrate delegation.

## 2. User authorization and model discovery
- Before every new Worker job, clearly show the active Project root, proposed Worker Role, exact Model ID, and session strategy. Start only after the user explicitly authorizes that combination, unless the current user instruction already explicitly specifies it.
- Authorization is enforced by Supervisor policy, not by a hard code-level lock inside MCP. Never claim that the backend itself proves a user approved a Worker invocation.
- Never silently choose a Role or Model. Never infer authorization for a new job from a previous job.
- `opencode_go_inspect` is on-demand, not a mandatory preflight for every job. Call it when the user asks for the model catalog/workspace allowance, when no previously inspected catalog or user-selected model is available, or when a selected model is rejected / catalog state is genuinely uncertain.
- When the user has selected a known model, skip redundant Inspect calls. `opencode_go_start` independently checks the live model catalog and required Agent before creating a Session; an invalid selection fails before prompt submission.
- Use `output_mode="summary"` for routine Inspect. Use `full` only for an explicit diagnostic request.
- When Inspect is requested, list all currently active `opencode-go` model IDs. Explain that Workspace Allowance is workspace-level, not a per-model balance. Do not repeat the full catalog in every subsequent job prompt.

## 3. Role and Session identity
- `analyst` and `reviewer` are read-only (`explore`; edit/shell denied). `writer` and `reviser` use `build` with project-scoped edits and one-time shell approval. `general` requires approval for edits and shell. All roles deny nested subagents.
- A Worker Slot is bound to its current Session and exact Role/Model. `reuse` may target only that Slot's current Session. `fork` may explicitly branch from a parent Session recorded under another Slot in the same Project, but the Bridge must verify the parent's Project/Role/Model from Job history. The requested Role/Model must match the parent; if the destination Slot already exists, its Role/Model must also match. The forked child becomes the destination Slot's current Session.
- If Role or Model changes, use `session_strategy="new"` or a different Worker Slot. Never use Reuse/Fork to pretend that a Session has switched models or permissions.
- Prefer `reuse` for a continuous task with the same Slot/Role/Model. Use `new` for a new task direction or changed binding. Use `fork` only to branch a completed, non-busy Session while preserving its exact Role/Model context.
- Do not call Session Inspect or cost prediction for every small task. Use Session Inspect for long-running work, suspected context bloat, or uncertain Slot health. Cost prediction is a heuristic aid, not a billing quote; call it only when the routing decision is genuinely unclear.
- Save a concise structured Slot summary at meaningful phase boundaries; do not store transcripts or duplicate the full project documentation.

## 4. Execution and reporting
- Prefer `opencode_go_wait_result` over Supervisor polling loops. Use `opencode_go_status` only when an immediate permission/state check is useful.
- After terminal completion, call `opencode_go_result` once. It is idempotent and returns the observed usage breakdown.
- Workers should write extensive reports, code, and other deliverables directly to files within the Project. Return concise findings and relative paths rather than reproducing long artifacts in chat.
- Report Job ID, Session ID, Project, Role, Model, terminal state, deliverable paths, and observed usage. Keep `input`, `output`, `reasoning`, `cache_read`, `cache_write`, and `cost_usd` separate; never merge reasoning into output or cache reads into ordinary input.
- Do not call cumulative `opencode_go_usage_report` after every job. Use it when the user asks for cumulative usage.

## 5. Security invariants
- Never start a Worker without explicit user authorization for its Role/Model/Project, except when already explicitly provided in the current instruction.
- Never auto-escalate permissions or approve shell actions without the user's explicit one-time approval.
- Never access files outside the active Project root, conceal Worker usage, or retry an ambiguous submission blindly."""

TOOLS: list[dict[str, Any]] = [
    {
        "name": "opencode_go_inspect",
        "description": (
            "按需只读检查当前 Antigravity Project 的 OpenCode Go 模型目录和 Workspace Allowance；不是每个 Worker Job 的强制前置步骤。"
            "project_root 必须由 Antigravity 从当前活动 Project 的真实根目录传入；绝不能硬编码或猜测路径。"
            "只返回 providerID=opencode-go 的模型。常规使用 output_mode=summary；仅在用户要求完整诊断时使用 full。"
        ),
        "inputSchema": {
            "type": "object", "properties": {"project_root": {"type": "string", "description": "当前 Antigravity 活动 Project 的绝对根目录。"},
                "output_mode": {"type": "string", "enum": ["summary", "full"], "default": "summary", "description": "输出模式：summary(默认，精简模型与用量摘要，节省 Token) 或 full(完整详情)"}},
            "required": ["project_root"], "additionalProperties": False,
        },
    },
    {
        "name": "opencode_go_start",
        "description": (
            "创建一个独立 OpenCode Go Worker Session，并异步提交自然语言任务。每个 Worker 一个独立 Session。"
            "必须传入当前 Project 根目录、用户明确授权的 Role 与 Model。Inspect 为按需操作，不要求每个 Job 前重复调用；本工具会在创建 Session 前实时校验模型和所需 Agent。"
            "Reuse 只能继续目标 Slot 当前 Session；Fork 可显式指定同一 Project 其他 Slot 的父 Session，但 Bridge 会用历史 Job 记录验证父 Session 的 Project/Role/Model，并检查目标 Slot（若已存在）的 Role/Model。Fork 子 Session 绑定到目标 Slot。Role/Model 不匹配时拒绝。"
            "role 可选 writer/reviewer/reviser/analyst/general。默认最多 3 个并发 Worker；只有用户明确要求增加并发时才允许 allow_over_limit=true，硬上限 6。"
            "project_root 必须是当前 Antigravity 活动 Project 根目录。Worker 禁止访问根目录外的文件、禁止嵌套 subagent；Reviewer/Analyst 禁止编辑且禁止 Shell；Writer/Reviser 的 Shell 操作逐项要求批准。"
            "返回 job_id 后使用 status/result 跟踪，不要重复提交同一任务。Antigravity 应自主规划角色与顺序，但服从用户明确指定。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_root": {"type": "string", "description": "当前 Antigravity 活动 Project 的绝对根目录。"},
                "role": {"type": "string", "enum": ["writer", "reviewer", "reviser", "analyst", "general"]},
                "task": {"type": "string", "description": "Worker 的完整自然语言任务和验收标准。"},
                "model_id": {"type": "string", "description": "用户从最近一次 inspect 返回的 opencode-go 模型中选择的 ID。可传短 ID 或 opencode-go/短 ID。"},
                "title": {"type": "string", "description": "可选 Session 标题。"},
                "allow_over_limit": {"type": "boolean", "default": False, "description": "只有用户明确要求超过默认 3 并发时才设 true；不能由 Agent 为方便自行开启。"},
                "worker_slot": {"type": "string", "description": "长期逻辑角色槽位（如 Worker_A、Lead_Writer 等）；缺省默认为 role。"},
                "session_strategy": {"type": "string", "enum": ["new", "reuse", "fork"], "default": "new", "description": "Session 路由策略：new(新建), reuse(复用当前槽位), fork(从已有会话分支)。"},
                "session_id": {"type": "string", "description": "显式指定复用或 Fork 的目标 session_id（可选）。"},
                "decision_reason": {"type": "string", "description": "Anti 选择该 Session 策略的自主决策理由。"},
                "benchmark_manifest": {"type": "object", "description": "基准测试不可变 Manifest 元数据（可选，记录不可变输入与参数）"},
                "estimated_cost": {"type": "object", "description": "Anti 预测的成本结构及分项数据（可选）。"},
                "prediction_confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0, "description": "预测置信度（可选）。"},
            },
            "required": ["project_root", "role", "task", "model_id"], "additionalProperties": False,
        },
    },
    {
        "name": "opencode_go_session_inspect",
        "description": "轻量获取指定 Worker Slot 或 Session 的历史用量、上下文限制、活跃状态与研究摘要，为 Session 策略决策提供数据。必须传入 project_root。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_root": {"type": "string", "description": "当前 Antigravity 活动 Project 的绝对根目录。"},
                "worker_slot": {"type": "string", "description": "逻辑角色槽位（如 Worker_A、writer 等）。"},
                "session_id": {"type": "string", "description": "具体 Session ID（可选）。"},
            },
            "required": ["project_root"], "additionalProperties": False,
        },
    },
    {
        "name": "opencode_go_session_predict_cost",
        "description": "评估当前任务在 Reuse、New、Fork 三种策略下的预期 Token 消耗、缓存效益及返工风险。只读决策工具，不调用模型。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_root": {"type": "string", "description": "当前 Antigravity 活动 Project 的绝对根目录。"},
                "worker_slot": {"type": "string", "description": "逻辑角色槽位（如 Worker_A、writer 等）。"},
                "task": {"type": "string", "description": "当前准备执行的任务自然语言描述。"},
                "model_id": {"type": "string", "description": "拟使用的模型 ID（可选）。"},
                "candidate_strategy": {"type": "string", "enum": ["new", "reuse", "fork"], "description": "重点评估的候选策略（可选）。"},
            },
            "required": ["project_root", "worker_slot", "task"], "additionalProperties": False,
        },
    },
    {
        "name": "opencode_go_session_update_summary",
        "description": "持久化记录指定 Worker Slot 的结构化研究摘要、证据出处与核心结论，供后续任务或 Session 迁移时继承。必须传入 project_root。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_root": {"type": "string", "description": "当前 Antigravity 活动 Project 的绝对根目录。"},
                "worker_slot": {"type": "string", "description": "逻辑角色槽位。"},
                "summary": {"type": "string", "description": "结构化研究摘要、文献与文件路径出处及关键待办。"},
            },
            "required": ["project_root", "worker_slot", "summary"], "additionalProperties": False,
        },
    },
    {
        "name": "opencode_go_predict_session_cost",
        "description": "评估当前任务在 Reuse、New、Fork 三种策略下的预期 Token 消耗、缓存效益及返工风险。只读决策工具，不调用模型。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_root": {"type": "string", "description": "当前 Antigravity 活动 Project 的绝对根目录。"},
                "worker_slot": {"type": "string", "description": "逻辑角色槽位（如 Worker_A、writer 等）。"},
                "task": {"type": "string", "description": "当前准备执行的任务自然语言描述。"},
                "model_id": {"type": "string", "description": "拟使用的模型 ID（可选）。"},
                "candidate_strategy": {"type": "string", "enum": ["new", "reuse", "fork"], "description": "重点评估的候选策略（可选）。"},
            },
            "required": ["project_root", "worker_slot", "task"], "additionalProperties": False,
        },
    },
    {
        "name": "opencode_go_update_session_summary",
        "description": "持久化记录指定 Worker Slot 的结构化研究摘要、证据出处与核心结论，供后续任务或 Session 迁移时继承。必须传入 project_root。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_root": {"type": "string", "description": "当前 Antigravity 活动 Project 的绝对根目录。"},
                "worker_slot": {"type": "string", "description": "逻辑角色槽位。"},
                "summary": {"type": "string", "description": "结构化研究摘要、文献与文件路径出处及关键待办。"},
            },
            "required": ["project_root", "worker_slot", "summary"], "additionalProperties": False,
        },
    },
    {
        "name": "opencode_go_wait_result",
        "description": (
            "内部等待当前 Antigravity Project 内指定 Worker 执行完成并返回最终结果。避免 Supervisor 高频轮询 status。"
            "若在等待期间出现待审批权限，会立即返回待审批状态；若在超时前完成，返回完整 result；若超时仍未完成，返回当前运行状态。"
            "必须传入当前 Project 根目录以防跨项目访问。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_root": {"type": "string", "description": "当前 Antigravity 活动 Project 的绝对根目录。"},
                "job_id": {"type": "string", "description": "Worker 任务 ID。"},
                "timeout_seconds": {"type": "integer", "default": 60, "minimum": 1, "maximum": 300, "description": "内部等待超时秒数，默认 60 秒。"},
            },
            "required": ["project_root", "job_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "opencode_go_status",
        "description": "查询 Worker 执行状态和待审批权限。运行或等待权限期间采用轻量检查，不读取完整消息历史；此时 usage_so_far 和 last_assistant_excerpt 为 null。任务终止后调用 opencode_go_result 获取完整回复和最终 Token 用量。必须传入当前 Project 根目录以防跨项目访问。",
        "inputSchema": {"type": "object", "properties": {"project_root": {"type": "string"}, "job_id": {"type": "string"}}, "required": ["project_root", "job_id"], "additionalProperties": False},
    },
    {
        "name": "opencode_go_result",
        "description": "获取当前 Antigravity Project 内指定 Worker 的完整 assistant 回复、各条 assistant message 的模型/Token/费用，以及当前状态。必须传入当前 Project 根目录，防止跨项目访问。",
        "inputSchema": {"type": "object", "properties": {"project_root": {"type": "string"}, "job_id": {"type": "string"}}, "required": ["project_root", "job_id"], "additionalProperties": False},
    },
    {
        "name": "opencode_go_list_jobs",
        "description": "列出当前 Antigravity Project 根目录下的最近 Worker 任务。project_root 必填；不提供跨项目列表。",
        "inputSchema": {"type": "object", "properties": {"project_root": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 30},
                "output_mode": {"type": "string", "enum": ["summary", "full"], "default": "summary", "description": "输出模式：summary(默认精简列表) 或 full(包含完整 terminal_result)"}}, "required": ["project_root"], "additionalProperties": False},
    },
    {
        "name": "opencode_go_permissions",
        "description": "查询当前 Antigravity Project 内某个 Worker 的待处理权限请求。Antigravity 应展示动作和资源，再根据用户明确决定回应。",
        "inputSchema": {"type": "object", "properties": {"project_root": {"type": "string"}, "job_id": {"type": "string"}}, "required": ["project_root", "job_id"], "additionalProperties": False},
    },
    {
        "name": "opencode_go_permission_reply",
        "description": "回应当前 Antigravity Project 内某个 Worker 的权限请求。decision 只能是 once 或 reject；不提供永久 always。只有用户明确批准后才能选择 once。",
        "inputSchema": {"type": "object", "properties": {"project_root": {"type": "string"}, "job_id": {"type": "string"}, "request_id": {"type": "string"}, "decision": {"type": "string", "enum": ["once", "reject"]}, "message": {"type": "string"}}, "required": ["project_root", "job_id", "request_id", "decision"], "additionalProperties": False},
    },
    {
        "name": "opencode_go_cancel",
        "description": "中断当前 Antigravity Project 内指定 Worker。仅在用户要求终止或有正当理由时调用。",
        "inputSchema": {"type": "object", "properties": {"project_root": {"type": "string"}, "job_id": {"type": "string"}}, "required": ["project_root", "job_id"], "additionalProperties": False},
    },
    {
        "name": "opencode_go_usage_report",
        "description": "查询并汇总 Antigravity Token Meter 与 OpenCode Worker Manager 的双平台 Token 用量报告。只读工具，仅在用户询问 Token 消耗、用量统计或要求生成用量报告时调用。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "period": {
                    "type": "string",
                    "enum": ["all", "30d", "7d"],
                    "default": "all",
                    "description": "统计时间周期：all (全部记录), 30d (最近30天), 7d (最近7天)。默认为 all。",
                },
                "include_recent": {
                    "type": "boolean",
                    "default": True,
                    "description": "是否在报告中包含最近执行的 Worker Job 简要列表。默认为 true。",
                },
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "opencode_go_benchmark_manifest",
        "description": "记录或查询不可变基准测试 Manifest 元数据。支持根据 manifest_id 或 job_id 查询参数与输入文件 SHA256。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["get", "record", "list"], "default": "get"},
                "identifier": {"type": "string", "description": "查询时的 manifest_id 或 job_id"},
                "manifest": {"type": "object", "description": "录入时的 Manifest 数据对象"}
            },
            "required": ["action"],
            "additionalProperties": False,
        },
    },
]


def _json_result(value: Any, is_error: bool = False) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False, indent=2, default=str)}],
        "structuredContent": value if isinstance(value, dict) else {"result": value},
        "isError": is_error,
    }


def handle_tool(name: str, args: dict[str, Any]) -> dict[str, Any]:
    if name == "opencode_go_session_inspect":
        return _json_result(CLIENT.session_inspect(
            raw_root=args["project_root"],
            worker_slot=args.get("worker_slot"),
            session_id=args.get("session_id"),
        ))
    if name in ("opencode_go_session_predict_cost", "opencode_go_predict_session_cost"):
        return _json_result(CLIENT.predict_session_cost(
            raw_root=args["project_root"],
            worker_slot=args["worker_slot"],
            task=args["task"],
            model_id=args.get("model_id"),
            candidate_strategy=args.get("candidate_strategy"),
        ))
    if name in ("opencode_go_session_update_summary", "opencode_go_update_session_summary"):
        return _json_result(CLIENT.update_session_summary(
            raw_root=args["project_root"],
            worker_slot=args["worker_slot"],
            summary=args["summary"],
        ))
    if name == "opencode_go_inspect":
        return _json_result(CLIENT.models_and_usage(args["project_root"], output_mode=args.get("output_mode", "summary")))
    if name == "opencode_go_start":
        start_args = dict(args)
        if "project_root" not in start_args:
            return _json_result({"error": "缺少必填参数 project_root。"}, is_error=True)
        raw_root = start_args.pop("project_root")
        manifest_meta = start_args.pop("benchmark_manifest", None)

        if manifest_meta is not None and not isinstance(manifest_meta, dict):
            return _json_result({"error": "非法参数：benchmark_manifest 必须是字典对象(object/dict)。"}, is_error=True)

        start_args["raw_root"] = raw_root

        # Support both new Client signature (with benchmark_manifest) and legacy Client (without it)
        import inspect
        try:
            sig = inspect.signature(CLIENT.start_worker)
            accepts_manifest = "benchmark_manifest" in sig.parameters or any(
                p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
            )
        except Exception:
            accepts_manifest = False

        if accepts_manifest:
            start_args["benchmark_manifest"] = manifest_meta
            res = CLIENT.start_worker(**start_args)
        else:
            # Legacy Client without benchmark_manifest: decouple and record via JobStore if provided
            res = CLIENT.start_worker(**start_args)
            if manifest_meta and isinstance(manifest_meta, dict) and hasattr(CLIENT, "record_benchmark_manifest"):
                try:
                    CLIENT.record_benchmark_manifest(
                        benchmark_name=manifest_meta.get("benchmark_name", "benchmark"),
                        benchmark_phase=manifest_meta.get("benchmark_phase", "unknown"),
                        model_id=start_args.get("model_id", ""),
                        role=start_args.get("role", ""),
                        session_strategy=start_args.get("session_strategy", "new"),
                        prompt_file=manifest_meta.get("prompt_file"),
                        input_file=manifest_meta.get("input_file"),
                        parameters=manifest_meta.get("parameters"),
                        job_id=res.get("job_id"),
                        session_id=res.get("session_id"),
                    )
                except Exception:
                    pass
        return _json_result(res)
    if name == "opencode_go_wait_result":
        return _json_result(CLIENT.wait_result(
            job_id=args["job_id"],
            raw_root=args["project_root"],
            timeout_seconds=args.get("timeout_seconds", 60),
        ))
    if name == "opencode_go_status":
        return _json_result(CLIENT.job_status(args["job_id"], args["project_root"]))
    if name == "opencode_go_result":
        return _json_result(CLIENT.job_result(args["job_id"], args["project_root"]))
    if name == "opencode_go_list_jobs":
        return _json_result(CLIENT.list_jobs(args["project_root"], args.get("limit", 30), output_mode=args.get("output_mode", "summary")))
    if name == "opencode_go_permissions":
        return _json_result(CLIENT.list_permissions(args["job_id"], args["project_root"]))
    if name == "opencode_go_permission_reply":
        return _json_result(CLIENT.reply_permission(args["job_id"], args["project_root"], args["request_id"], args["decision"], args.get("message")))
    if name == "opencode_go_cancel":
        return _json_result(CLIENT.cancel_worker(args["job_id"], args["project_root"]))
    if name == "opencode_go_usage_report":
        return _json_result(CLIENT.usage_report(
            period=args.get("period", "all"),
            include_recent=args.get("include_recent", True),
        ))
    if name == "opencode_go_benchmark_manifest":
        action = args.get("action", "get")
        if action == "get":
            return _json_result(CLIENT.get_benchmark_manifest(args["identifier"]))
        elif action == "record":
            mf = args["manifest"]
            return _json_result(CLIENT.record_benchmark_manifest(**mf))
        elif action == "list":
            return _json_result({"manifests": CLIENT.store.list_manifests(args.get("limit", 50))})
        return _json_result({"error": f"Unknown manifest action: {action}"}, True)
    return _json_result({"error": f"Unknown tool: {name}"}, True)


def send(message: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def dispatch(message: dict[str, Any]) -> None:
    method = message.get("method")
    request_id = message.get("id")
    params = message.get("params") or {}

    if method == "notifications/initialized" or method == "notifications/cancelled" or method == "notifications/progress":
        return
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": request_id, "result": {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            "instructions": SERVER_INSTRUCTIONS,
}})
        return
    if method == "ping":
        send({"jsonrpc": "2.0", "id": request_id, "result": {}})
        return
    if method == "tools/list":
        send({"jsonrpc": "2.0", "id": request_id, "result": {"tools": TOOLS}})
        return
    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments") or {}
        try:
            result = handle_tool(name, arguments)
        except BridgeError as exc:
            err_payload = exc.to_dict()
            result = _json_result(err_payload, True)
        except TypeError as exc:
            err_payload = {
                "error": f"TypeError: {exc}",
                "error_category": ErrorCategory.APPLICATION_ERROR,
                "user_message": f"参数调用契约错误: {exc}。请检查参数名与类型是否符合 MCP Schema，切勿终止或重启 MCP 进程！",
                "retryable": False,
                "circuit_break": False,
                "action_advice": "核对 tools/list 中该工具的 inputSchema 参数定义后重新传参。",
            }
            result = _json_result(err_payload, True)
        except (KeyError, ValueError) as exc:
            err_payload = {
                "error": f"{type(exc).__name__}: {exc}",
                "error_category": ErrorCategory.APPLICATION_ERROR,
                "user_message": f"参数校验错误: {exc}。切勿终止或重启 MCP 进程！",
                "retryable": False,
                "circuit_break": False,
                "action_advice": "核对参数必填项与有效取值后重试。",
            }
            result = _json_result(err_payload, True)
        except urllib.error.URLError as exc:
            tripped = CLIENT.circuit_breaker.record_failure(ErrorCategory.TRANSPORT_ERROR)
            err_payload = {
                "error": f"URLError: {exc}",
                "error_category": ErrorCategory.RECOVERY_FAILED if tripped else ErrorCategory.TRANSPORT_ERROR,
                "user_message": f"与 OpenCode 守护进程通信失败: {exc}。请确认 127.0.0.1 OpenCode Server 端口是否存活，切勿强杀 Python MCP Server 进程！",
                "retryable": not tripped,
                "circuit_break": tripped,
                "action_advice": "检查后台 opencode.exe 是否正在运行，检查端口是否被占用。",
            }
            result = _json_result(err_payload, True)
        except TimeoutError as exc:
            tripped = CLIENT.circuit_breaker.record_failure(ErrorCategory.TIMEOUT)
            err_payload = {
                "error": f"TimeoutError: {exc}",
                "error_category": ErrorCategory.RECOVERY_FAILED if tripped else ErrorCategory.TIMEOUT,
                "user_message": f"请求超时: {exc}。切勿强杀 MCP 进程！",
                "retryable": not tripped,
                "circuit_break": tripped,
                "action_advice": "等待几秒后查询 job_status 或重试。",
            }
            result = _json_result(err_payload, True)
        except Exception as exc:
            print(f"Unhandled tool error ({name}): {exc}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            err_payload = {
                "error": f"{type(exc).__name__}: {exc}",
                "error_category": ErrorCategory.APPLICATION_ERROR,
                "user_message": f"内部处理异常: {exc}。切勿强杀 MCP 进程！",
                "retryable": False,
                "circuit_break": False,
                "action_advice": "检查错误堆栈并修正调用逻辑。",
            }
            result = _json_result(err_payload, True)
        send({"jsonrpc": "2.0", "id": request_id, "result": result})
        return

    if request_id is not None:
        send({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": f"Method not found: {method}"}})


def _configure_stdio_utf8() -> None:
    # MCP stdio messages are UTF-8. Do not inherit Windows ANSI/GBK
    # encoding when Antigravity communicates through redirected pipes.
    for stream_name, errors in (
        ("stdin", "strict"),
        ("stdout", "backslashreplace"),
        ("stderr", "backslashreplace"),
    ):
        stream = getattr(sys, stream_name, None)
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors=errors)


def main() -> int:
    _configure_stdio_utf8()
    try:
        for line in sys.stdin:
            raw = line.strip()
            if not raw:
                continue
            try:
                message = json.loads(raw)
                if isinstance(message, dict):
                    dispatch(message)
            except Exception as exc:
                print(f"JSON-RPC input error: {exc}", file=sys.stderr, flush=True)
    finally:
        shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
