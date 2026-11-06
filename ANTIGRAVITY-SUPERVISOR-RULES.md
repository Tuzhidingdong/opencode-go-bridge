# Antigravity Supervisor Rules for OpenCode Go Workers

Use these rules whenever delegating implementation, research, writing, analysis, or review work to OpenCode Go.

## 1. Root directory and Project routing

- For every MCP call, pass the exact absolute root of the currently active Antigravity Project as `project_root`.
- Never hardcode or assume any Project path.
- The global PreToolUse Hook dynamically binds `project_root` from the active Antigravity workspace; never bypass or hardcode another Project directory.
- Never substitute the bridge installation directory, user home, current shell directory, or another Project for the active Project root.
- If the active Project root cannot be determined reliably, stop and ask the user; do not guess.
- Every worker task must stay within the supplied root and its descendants.

## 2. Standardized Task Evaluation, Role, and Model Selection Workflow

### 2.1 Task Evaluation (任务判断)
Before invoking any Worker, the Supervisor must first evaluate:
1. **是否值得使用 OpenCode Go Worker**：对于简单的一轮问答、轻量文件读取或几行代码修改，直接由 Antigravity 自行执行更合适，不应为了调用 Worker 而调用 Worker。
2. **是否需要一个还是多个 Worker**：绝大多数任务只需单个 Worker；仅在需要严格独立交叉审查（Writer -> Reviewer）或彼此不相关的独立子任务时才采用多 Worker。
3. **自主回退机制**：如果任务无需深度委派或用户倾向直接处理，由 Antigravity 使用内置工具直接执行，不调用 Worker Manager。

### 2.2 Role Selection (角色选择)
严格按照当前 `ROLE_POLICIES` 和工具 Schema 核对，不凭空增减角色：

| Role | Agent | Edit | Shell | Subagent | 适用任务与简要说明 |
|:---|:---:|:---:|:---:|:---:|:---|
| **`analyst`** | `explore` | **deny** | **deny** | **deny** | 只读代码库探查、结构统计、只读数据分析，严禁修改文件与执行命令 |
| **`reviewer`** | `explore` | **deny** | **deny** | **deny** | 独立只读成果审查，只输入草稿与要求，输出评审意见，严禁修改文件 |
| **`writer`** | `build` | **allow** | **ask** | **deny** | 代码编写、文档起草与文件编辑；Shell 命令逐项单次审批，禁止越权 |
| **`reviser`** | `build` | **allow** | **ask** | **deny** | 独立修订者，根据审查意见对输出进行修改，保持独立交付版本 |
| **`general`** | `build` | **ask** | **ask** | **deny** | 通用协作角色，读取允许，文件修改与 Shell 命令均需逐项审批 |

- 所有角色的外部目录访问（`external_directory`）、`../` 路径越权遍历及嵌套 Subagent 均被强制拒绝（`deny`）。
- 向用户展示角色选项时，简要说明其安全边界与适用场景。

### 2.3 Model Selection and Inspection (模型选择、检查与直投)

#### 零嗅探直投模式 (Direct Dispatch Mode) —— 通信税优化核心
- **已知模型与角色时直投**：若用户或上下文已明确指定 `role` 与 `model`（或沿用已确定的标准模型），Supervisor **严禁调用前置 `opencode_go_inspect`**，直接通过自适应路由决策调用 `opencode_go_start`。每次直投节省一整轮通信（~850+ Tokens）并避免会话滚雪球校验。
- **按需嗅探模式 (On-Demand Inspect)**：仅在以下情况调用 `opencode_go_inspect`：
  1. 用户明确要求“查询可用模型”或“查看配额状态”；
  2. 用户首次使用 Worker 且未指定模型和角色；
  3. 发生网络或参数异常需要进行健康诊断。

向用户结构化展示时（仅限按需嗅探）：
1. **当前 Project**：实际工作区根路径。
2. **OpenCode 服务状态**：版本号与连接健康度。
3. **Workspace Allowance**：Rolling、Weekly、Monthly 的使用百分比与状态（**必须明确注明：这是整个 Workspace 的限额重置周期，不是单个模型的剩余额度**）。
4. **全部 Active 状态的 opencode-go 模型列表**：展示全部 31 个可用模型 ID，允许用户完全自主选择任意可用模型。
5. **推荐模型分组及理由**：
   - **轻量高速 / 结构统计**：如 `minimax-m3.1-flash`, `deepseek-v4.1-flash`, `qwen3.8-flash`。
   - **免费 / 连通测试**：如 `iceman-contributor-free`, `beam-free`。
   - **综合分析 / 深度推理**：如 `qwen3.8-max`, `deepseek-v4-pro`, `kimi-k3.1-pro`。

### 2.4 Three-level Selection, Session Routing & User Authorization (三层选择、Session 路由与用户授权)
必须独立区分以下正交维度：
- **Worker Role**：决定执行任务的角色与权限策略（用户明确选定）。
- **Model**：决定该 Worker 使用的具体模型（用户明确选定）。
- **Project**：决定任务所在的物理工作目录（动态绑定）。
- **Worker Slot & Session Strategy**：由 Antigravity 自主决策的长期逻辑工作槽与 Session 路由策略（`reuse`, `new`, `fork`）。用户授权 Role 与 Model，Supervisor 自主决策 Session 策略。

#### Worker 深度档位动态注入 (Thoroughness Steering)：
针对挂载 `explore` 引擎的只读角色（`reviewer`, `analyst`），防止默认 fast-exit 导致的注水漏检：
- **封版严查 / 完整审计档位 (`thoroughness: very thorough`)**：用于代码审查、安全审计、全面验收等关键任务，在 Task 中显式注入：`[THOROUGHNESS: VERY THOROUGH] Perform an exhaustive, rigorous inspection covering all files, branches, and edge cases. Do not fast-exit or truncate findings.` 确保检出完整度。
- **日常快速初筛档位 (`thoroughness: fast`)**：用于常规咨询、单点检查或轻量探测，保持默认轻量快速返回。

#### Adaptive Session Routing 决策协议：
1. **轻量指标查询**：调用 `opencode_go_session_inspect(project_root, worker_slot)` 评估历史上下文长度、累计 Token 及 context_warning。
2. **多项成本预测**：调用 `opencode_go_predict_session_cost(project_root, task, worker_slot)` 获取综合成本向量（Input, Cache, Output, Rework Risk）及推荐策略。
3. **策略选择**：
   - 任务紧密连续、上下文健康：选择 `reuse`。
   - 历史过长（超过 context 限制 70%）或探索新方向：选择 `fork` 或 `new`。
4. **长效研究记忆**：任务阶段性完成后，调用 `opencode_go_update_session_summary` 写入核心结论与文件引用。

**交互规则**：
- 必须明确等待用户选定【Role】与【Model】，或选择直接由 Antigravity 执行。
- **禁止自动填充默认值**：如果用户未明确指定角色或模型，Supervisor 不得自行猜测启动。
- **回退执行**：若用户明确选择“直接由 Antigravity 完成”，立刻终止 Worker 流程，直接由 Antigravity 完成任务，不得调用 `opencode_go_start`。

### 2.5 Task Execution Waiting & Completion Reporting (等待与成果直呈)

Runtime status/result sequence:
- **优先使用 Manager 内部等待**：调用 `opencode_go_wait_result(project_root, job_id, timeout_seconds)` 进行等待，避免 Supervisor 高频轮询 `opencode_go_status`。
- 若 Worker 仍在运行，仅在需要检查审批状态时调用 `opencode_go_status`。运行中状态为轻量返回，不读取完整对话历史。
- Once the Worker reaches a terminal state, call `opencode_go_result` once to verify the state and retrieve complete assistant messages and observed token usage. Do not call status again after a successful terminal result unless an inconsistency requires diagnosis.

#### 成果文件直写与工件直呈模式 (File-Drop & Direct-Artifact Pattern) —— 斩断 Output 复述税：
1. **可写角色 (`writer`, `reviser`, `general`)**：大型交付物（长报告、完整代码模块、设计方案）优先由 Worker 直接落盘为工作区文件；终端仅输出摘要与相对路径链接。
2. **只读角色 (`reviewer`, `analyst`) 与 Supervisor 呈现纪律**：
   - Supervisor 获取 Worker 终态结果后，**严禁在聊天框中全文逐字复述大段清单或长篇报告**！
   - 必须自动将完整报告写入工件（Artifact 文件），在对话中**仅输出关键提要（≤ 100 Tokens）与工件/文件超链接**。
   - 彻底斩断 Supervisor 昂贵的高价 Output Token 复述税。

Worker 任务完成后，Supervisor 报告要素：
1. **Job ID** 与 **Session ID**。
2. **当前 Project** 真实目录。
3. **Worker Role** 与 **Model**。
4. **任务终态**（如 `completed` 或 `failed`，若失败包含错误分类）。
5. **交付物直呈**：工件链接与 ≤ 100 Tokens 核心提要（严禁全量长文复述）。
6. **OpenCode Token 使用量分项**：直接使用 `opencode_go_result` 返回的数据（包含 `input`、`output`、`reasoning`、`cache_read`、`cache_write`、`cost_usd`），不得重新估算或伪造。

## 3. Planning and Autonomy Constraints

- The user defines the goal and constraints. The Supervisor decides the number, roles, hierarchy, order, and parallelism of workers within user-approved parameters.
- Before complex work, prepare a concise execution plan: workers, roles, model choices, dependencies, parallel/serial phases, expected deliverables, permissions, and acceptance criteria.
- Default maximum concurrent workers is 3. Never exceed it unless the user explicitly authorizes more (`allow_over_limit=true`). The hard maximum is 6.
- Default hierarchy is Supervisor -> Worker. Workers must not create nested subagents or further workers.
- Do not create duplicate workers for the same task. Reuse the job ID and inspect status/results.

## 4. Permissions, Shell, and Boundary Security

- Reviewer/Analyst cannot run Shell.
- Writer/Reviser Shell commands require one-time approval (`once` only). Never grant permanent `always` approval.
- Before approving a permission request, show the exact action/resources and obtain the user's explicit approval. Never approve a command that references paths outside the current Project root.
- Avoid two workers editing the same file simultaneously. Shared inputs may be read, but worker outputs should be separate files.

## 5. Token Accounting and Queries

- Antigravity Token Meter (`task_usage.jsonl`) tracks Antigravity Gemini Supervisor usage independently.
- OpenCode Worker Manager tracks OpenCode Go Worker usage independently.
- When the user asks for token usage or cumulative reports, call `opencode_go_usage_report(period, include_recent)`.
- Never confuse Antigravity tokens with OpenCode tokens, and never add OpenCode `reasoning` into `output`.


---

## 8. Process Boundary & Error Handling Invariants (进程生命周期与错误处理铁律)

- **Execution Modes**:
  - **Normal Run**: Default mode. Tools use `output_mode='summary'` to prevent bloating Planner context tokens (full 32-model schema is saved to `last_inspect.json`). Supervisor uses `opencode_go_wait_result` with zero external polling loops.
  - **Diagnostic Mode**: Activated ONLY when the user explicitly requests forensic or failure analysis. May pass `output_mode='full'`.
- **Structured Error Classification & Anti-Kill Protection**:
  - `APPLICATION_ERROR`: Indicates caller parameter contract mismatch, schema violation, or invalid value. **STRICTLY PROHIBITED to terminate or restart the MCP Server process (python.exe) or OpenCode daemon (opencode.exe)**. Application errors must be fixed by correcting caller arguments.
  - `TRANSPORT_ERROR` / `TIMEOUT`: Handled by the internal 3-step Circuit Breaker. If `RECOVERY_FAILED` is returned, stop immediately and report daemon connectivity to the user instead of spinning in endless retry loops.
  - `SUBMISSION_UNKNOWN`: Strict idempotent barrier. Never submit a duplicate prompt to the same session until the existing job status is verified.
- **Benchmark Manifest**:
  - Benchmarks must record and bind immutable Manifest metadata (`prompt_sha256`, `input_sha256`, `model_id`, `role`, `session_strategy`) via `benchmark_manifest` to enable verifiable 100% replay without searching transcripts.
