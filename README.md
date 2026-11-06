# Antigravity + OpenCode Go Bridge

[![Tests](https://img.shields.io/badge/tests-99%20passed-brightgreen.svg)]()
[![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20Linux%20%7C%20macOS-blue.svg)]()
[![License](https://img.shields.io/badge/license-MIT-green.svg)]()

Industrial-grade bridge between **Google Antigravity IDE** (Supervisor) and **OpenCode Go** (Execution Workers), featuring:
- **Universal Pre-Execution Protocol & Intent Recognition**: Automatically separates conversational requests from actionable execution tasks.
- **Three Anti-Tax Disciplines**: Drops communication tax by >90% via Direct Dispatch, File-Drop/Artifact rendering, and Thoroughness steering.
- **Strict Role Permissions Matrix**: Multi-tiered boundaries (`analyst`, `reviewer`, `writer`, `reviser`, `general`) with guaranteed project root containment and subagent lockdown.
- **Token Usage Metering**: Real-time Stop & SessionStart hooks tracking Supervisor and Worker tokens independently.

---

## 🚀 Quick Setup on a New Machine (一键迁移与新机部署)

### 1. Prerequisites (前置准备)
- **Python 3.10+** (with `pip`)
- **OpenCode CLI**: Ensure `opencode` is installed and daemon is running (`opencode daemon` on `127.0.0.1:4096`).
- **Google Antigravity IDE**

### 2. Clone Repository
```bash
git clone https://github.com/Tuzhidingdong/opencode-go-bridge.git
cd opencode-go-bridge
```

### 3. Install Dependencies
```bash
pip install -r requirements.txt
```
*(Dependencies: `fastmcp`, `httpx`, `pytest`, `pytest-asyncio`)*

### 4. Run One-Click Auto-Installer
On Windows PowerShell:
```powershell
python install.py
```
Or:
```powershell
.\setup.ps1
```

The installer will automatically:
1. Copy Global Supervisor Governance Rules to `~/.gemini/AGENTS.md`.
2. Register the MCP Server in `~/.gemini/config/mcp_config.json` with dynamic path binding.
3. Configure Token Meter & Project Router hooks in `~/.gemini/config/hooks.json`.
4. Deploy the 14 MCP Tool Schemas to `~/.gemini/antigravity/mcp/opencode-go-worker-manager/`.
5. Run full local self-check test suite.

### 5. Restart Antigravity IDE
Restart or reload Antigravity IDE. You will see `opencode-go-worker-manager` available in your MCP tools list!

---

## 🧪 Running Tests (运行测试套件)

To verify all 99 integration and unit tests:
```bash
python -m pytest test_comprehensive_v2_hardening.py test_production_acceptance.py test_v2_backward_compatibility.py test_worker_lifecycle.py test_worker_manager.py test_usage_report.py test_adaptive_session_routing.py test_antigravity_opencode_project_router.py -q
```

---

## 📂 Architecture & Directory Layout

```
opencode-go-bridge/
├── opencode_go_worker_core.py      # Core worker management engine, session lifecycle & costs
├── opencode_go_worker_mcp.py       # FastMCP protocol bridge & server implementation
├── antigravity_opencode_project_router.py  # PreToolUse security router & auto-isolation
├── antigravity_usage_hook.py       # Antigravity Supervisor token meter hook
├── install.py                      # One-click environment installer for new machines
├── setup.ps1                       # PowerShell launcher
├── requirements.txt                # Python package dependencies
├── antigravity-templates/          # Global rules and schemas templates
│   ├── AGENTS.md                   # Global supervisor rules (Intent Recognition & Anti-Tax)
│   ├── mcp_config.json.template    # Dynamic MCP template
│   ├── hooks.json.template         # Dynamic Hooks template
│   └── mcp-schemas/                # 14 MCP tool schemas + instructions.md
└── test_*.py                       # Full 99-case test suites
```

---

## 📜 Security & Role Matrix

| Role | Mode | Edit | Shell | Subagent | Primary Use Case |
| :--- | :---: | :---: | :---: | :---: | :--- |
| **`analyst`** | Explore | `deny` | `deny` | `deny` | Read-only discovery & inspection |
| **`reviewer`** | Explore | `deny` | `deny` | `deny` | Independent blind code & file review |
| **`writer`** | Build | `allow` | `ask` | `deny` | Project-scoped code/doc editing |
| **`reviser`** | Build | `allow` | `ask` | `deny` | Project-scoped refactoring |
| **`general`** | Build | `ask` | `ask` | `deny` | Multi-step build tasks |

---

## 📄 License
MIT License.
