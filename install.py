#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Antigravity + OpenCode Go Bridge: One-Click Installer & Sync Utility
===================================================================
Automatically configures any new machine with:
  1. Global Agent Supervisor Rules (~/.gemini/AGENTS.md)
  2. MCP Server Registration (~/.gemini/config/mcp_config.json)
  3. Token Meter & Project Router Hooks (~/.gemini/config/hooks.json)
  4. Antigravity MCP Tool Schemas (~/.gemini/antigravity/mcp/opencode-go-worker-manager/)
  5. Local Self-Test & Verification
"""

import sys
import os
import shutil
import json
import subprocess
from pathlib import Path


def log(msg: str):
    print(f"[*] {msg}")


def log_ok(msg: str):
    print(f"[+] {msg}")


def log_err(msg: str):
    print(f"[-] {msg}")


def install():
    bridge_dir = Path(__file__).resolve().parent
    python_exe = sys.executable
    home_dir = Path.home()
    gemini_dir = home_dir / ".gemini"
    config_dir = gemini_dir / "config"
    mcp_dir = gemini_dir / "antigravity" / "mcp" / "opencode-go-worker-manager"
    templates_dir = bridge_dir / "antigravity-templates"

    print("=" * 70)
    print(" Antigravity + OpenCode Go Bridge: Environment Auto-Installer")
    print("=" * 70)
    log(f"Bridge Root Directory : {bridge_dir}")
    log(f"Current Python Exe    : {python_exe}")
    log(f"Target .gemini Root   : {gemini_dir}")

    if not templates_dir.exists():
        log_err(f"Templates directory not found: {templates_dir}")
        sys.exit(1)

    # 1. Ensure target directories exist
    config_dir.mkdir(parents=True, exist_ok=True)
    mcp_dir.mkdir(parents=True, exist_ok=True)

    # Helper for escaping paths in JSON
    bridge_dir_escaped = str(bridge_dir).replace("\\", "\\\\")
    python_exe_escaped = str(python_exe).replace("\\", "\\\\")

    # 2. Sync AGENTS.md
    src_agents = templates_dir / "AGENTS.md"
    dst_agents = gemini_dir / "AGENTS.md"
    if src_agents.exists():
        if dst_agents.exists():
            backup_path = gemini_dir / f"AGENTS.md.bak_{os.getpid()}"
            shutil.copy2(dst_agents, backup_path)
            log(f"Backed up existing AGENTS.md -> {backup_path.name}")
        shutil.copy2(src_agents, dst_agents)
        log_ok(f"Installed Global Rules -> {dst_agents}")

    # 3. Configure mcp_config.json
    mcp_tmpl_path = templates_dir / "mcp_config.json.template"
    dst_mcp_config = config_dir / "mcp_config.json"
    if mcp_tmpl_path.exists():
        with open(mcp_tmpl_path, "r", encoding="utf-8") as f:
            tmpl_content = f.read()
        rendered = tmpl_content.replace("{{PYTHON_EXECUTABLE}}", python_exe_escaped).replace("{{BRIDGE_DIR}}", bridge_dir_escaped)
        new_mcp_data = json.loads(rendered)

        if dst_mcp_config.exists():
            try:
                with open(dst_mcp_config, "r", encoding="utf-8") as f:
                    existing_data = json.load(f)
            except Exception:
                existing_data = {}
            if "mcpServers" not in existing_data:
                existing_data["mcpServers"] = {}
            existing_data["mcpServers"]["opencode-go-worker-manager"] = new_mcp_data["mcpServers"]["opencode-go-worker-manager"]
            final_data = existing_data
        else:
            final_data = new_mcp_data

        with open(dst_mcp_config, "w", encoding="utf-8") as f:
            json.dump(final_data, f, indent=2, ensure_ascii=False)
        log_ok(f"Configured MCP Server -> {dst_mcp_config}")

    # 4. Configure hooks.json
    hooks_tmpl_path = templates_dir / "hooks.json.template"
    dst_hooks_config = config_dir / "hooks.json"
    if hooks_tmpl_path.exists():
        with open(hooks_tmpl_path, "r", encoding="utf-8") as f:
            tmpl_content = f.read()
        rendered = tmpl_content.replace("{{BRIDGE_DIR}}", bridge_dir_escaped)
        new_hooks_data = json.loads(rendered)

        if dst_hooks_config.exists():
            try:
                with open(dst_hooks_config, "r", encoding="utf-8") as f:
                    existing_data = json.load(f)
            except Exception:
                existing_data = {}
            existing_data.update(new_hooks_data)
            final_hooks = existing_data
        else:
            final_hooks = new_hooks_data

        with open(dst_hooks_config, "w", encoding="utf-8") as f:
            json.dump(final_hooks, f, indent=2, ensure_ascii=False)
        log_ok(f"Configured Hooks -> {dst_hooks_config}")

    # 5. Copy MCP schemas & instructions
    schemas_dir = templates_dir / "mcp-schemas"
    if schemas_dir.exists():
        count = 0
        for item in schemas_dir.glob("*"):
            if item.is_file():
                shutil.copy2(item, mcp_dir / item.name)
                count += 1
        log_ok(f"Copied {count} MCP schema files & instructions -> {mcp_dir}")

    # 6. Auto-configure OpenCode Auth if key file is found
    key_candidates = [
        Path(r"E:\AI_Confinguration\API\Opencode_api.txt"),
        home_dir / "Opencode_api.txt",
        bridge_dir / "Opencode_api.txt",
    ]
    api_key = None
    for cand in key_candidates:
        if cand.exists():
            try:
                raw_k = cand.read_text(encoding="utf-8").strip()
                if raw_k:
                    api_key = raw_k
                    log_ok(f"Found OpenCode API key from {cand}")
                    break
            except Exception as e:
                log_err(f"Failed to read key from {cand}: {e}")

    if api_key:
        opencode_auth_dir = home_dir / ".local" / "share" / "opencode"
        opencode_auth_dir.mkdir(parents=True, exist_ok=True)
        auth_file = opencode_auth_dir / "auth.json"
        auth_payload = {
            "opencode": {
                "type": "api",
                "key": api_key
            },
            "opencode-go": {
                "type": "api",
                "key": api_key
            }
        }
        with open(auth_file, "w", encoding="utf-8") as f:
            json.dump(auth_payload, f, indent=2)
        log_ok(f"Configured OpenCode auth -> {auth_file}")
    else:
        log("No local Opencode_api.txt found; skipping OpenCode auth injection.")

    # 7. Verification
    print("-" * 70)
    log("Running self-check tests to verify installation integrity...")
    try:
        res = subprocess.run([python_exe, "-m", "pytest", "test_production_acceptance.py", "-q"], cwd=str(bridge_dir), capture_output=True, text=True)
        if res.returncode == 0:
            log_ok("Self-check passed: test_production_acceptance.py 100% OK!")
        else:
            log_err(f"Test warning: {res.stdout}\n{res.stderr}")
    except Exception as e:
        log_err(f"Could not run self-check: {e}")

    print("=" * 70)
    log_ok("INSTALLATION COMPLETE! Please restart or reload Antigravity IDE.")
    print("=" * 70)


if __name__ == "__main__":
    install()
