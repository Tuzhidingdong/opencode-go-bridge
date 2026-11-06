# OpenCode Go Worker Manager (0.2.0)

A local stdio MCP server for Antigravity. It orchestrates multiple independent OpenCode Go sessions, with per-session model selection, asynchronous execution, status/result retrieval, token accounting, permission handling, project-root scoping, lifecycle safety management, and Adaptive Session Routing.

## Core Design & Lifecycle Management

- **Isolated Process Architecture**: One shared OpenCode installation and one per-MCP-process local OpenCode Server (`127.0.0.1`, random port, random in-memory Basic Auth password).
- **Windows Job Object Lifecycle Containment**: Child OpenCode server processes are automatically assigned to a Win32 Job Object with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`. When the Worker Manager process exits (normally or abnormally), all associated child processes are guaranteed to terminate without orphan leaks.
- **Anti-PID-Reuse Protection**: Process alive checks combine PID with process creation timestamp (`GetProcessTimes`), completely preventing accidental termination of unrelated processes if a PID is reused by Windows.
- **Startup Reconciliation**: On startup, Manager automatically reconciles unfinalized jobs (`submitted`, `running`, `idle_no_response`) against real running processes and session status. Vanished jobs safely converge to `interrupted` without ghost token consumption or false running states.
- **Internal Wait Mechanism (`opencode_go_wait_result`)**: Instead of Supervisor polling status in high frequency, the Manager provides an internal wait loop with configurable timeout and interval, immediately returning upon terminal completion or permission request.
- **Idempotent Retrieval and Cancellation**: `opencode_go_result` caches terminal responses in SQLite companion table `job_process`; repeated calls return immediately without touching external APIs. `opencode_go_cancel` verifies real session interruption and is safe to call repeatedly.
- **Zero Real Token Consumption in Tests**: All lifecycle, routing, and error tests run against mock/fake worker infrastructure.

## Adaptive Session Routing (自适应 Session 路由)

Antigravity (Supervisor) autonomously manages worker sessions instead of fixing sessions to always-new or always-reuse:

1. **Job vs. Session vs. Worker Slot**:
   - `Job`: A single execution task with unique `job_id`.
   - `Session`: A conversational context thread (`session_id`).
   - `Worker Slot`: A persistent logical role within a project (e.g. `Lead_Writer`, `Code_Reviewer`), mapped via `(project_root, worker_slot)`. Multiple successive Jobs can safely reuse the same Session.
2. **Autonomous Strategies**:
   - `reuse`: Continue conversation in the slot's current Session.
   - `new`: Create a fresh Session with minimal context.
   - `fork`: Branch from an existing Session to explore an alternative line while preserving historical context.
3. **Session-Level Concurrency Lock**:
   - Sessions are protected by in-memory concurrency locks (`_running_sessions`). Attempting to start multiple concurrent jobs on the same session is strictly denied.
4. **Cross-Project Isolation**:
   - Sessions and slots are strictly scoped to `project_root`. Cross-project session reuse is prohibited.
5. **Multi-Term Cost Prediction Model**:
   - Predicts strategy costs based on: Input tokens, Cache Read tokens, Cache Write tokens, Output tokens, Context preparation, and expected Rework risk factor.
   - Evaluates context bloat (warning triggered at >70% context limit) and recommends Fork/New when bloated.
6. **Telemetry & Feedback Loop**:
   - Every routing decision is persisted in `session_routing_decisions`. Upon job completion, actual token consumption is recorded to calibrate future predictions.

## MCP Tools (13 Tools)

1. `opencode_go_inspect(project_root)` — discover Go models and Go workspace allowance.
2. `opencode_go_start(project_root, role, task, model_id, worker_slot?, session_strategy?, session_id?, ...)` — start a worker with adaptive session strategy (`new`/`reuse`/`fork`).
3. `opencode_go_status(project_root, job_id)` — lightweight check for execution state and pending permissions.
4. `opencode_go_result(project_root, job_id)` — retrieve assistant messages and message-level token usage. Idempotent and cached.
5. `opencode_go_wait_result(project_root, job_id, timeout_seconds?, poll_interval?)` — wait inside Manager for completion or permission request, eliminating Supervisor polling loops.
6. `opencode_go_list_jobs(project_root, limit?)` — list recent jobs for this Project only.
7. `opencode_go_permissions(project_root, job_id)` — list pending permission requests.
8. `opencode_go_permission_reply(project_root, job_id, request_id, decision)` — approve once or reject.
9. `opencode_go_cancel(project_root, job_id)` — interrupt a worker with verified stop confirmation.
10. `opencode_go_usage_report(period?, include_recent?)` — dual-platform token usage reports for Antigravity Token Meter and OpenCode Worker Manager.
11. `opencode_go_session_inspect(project_root, worker_slot?, session_id?)` — lightweight inspection of slot metrics, cumulative tokens, context limits, and bloat warnings.
12. `opencode_go_predict_session_cost(project_root, task, worker_slot?, model_id?)` — multi-term cost prediction and strategy recommendation.
13. `opencode_go_update_session_summary(project_root, worker_slot, summary)` — persist structured research conclusions and slot memory.

## Database Schema (Isolated Companion Architecture)

Stored in `%APPDATA%\OpenCodeGoWorkerManager\worker_jobs.sqlite3`:
- `jobs`: Core job execution table (10 columns, backward compatible).
- `job_process`: Lifecycle companion table (`server_pid`, `server_start_time`, `terminal_result`).
- `worker_slots`: Logical slot mapping (`project_root`, `worker_slot`, `current_session_id`, `role`, `model_id`, `summary`, `summary_updated_at`).
- `session_routing_decisions`: Strategy decision telemetry and actual usage feedback loop.

## Requirements

- Windows 10/11.
- Python 3.10+ (tested on Python 3.13.9).
- OpenCode V2 installed and authenticated with `opencode-go` Device OAuth.
- Standard library only (`sqlite3`, `ctypes`, `urllib`, `json`). Zero external dependencies.
