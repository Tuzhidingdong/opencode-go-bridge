#!/usr/bin/env python3
"""Low-overhead Antigravity transcript token collector.

- No network calls and no model calls.
- Reads only newly appended, complete JSONL transcript records at Stop time.
- Groups PLANNER_RESPONSE usage under the latest USER_INPUT.
- Writes one JSONL ledger row per completed task.
- Writes a small local diagnostic log without recording prompt/response content.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

APP_DIR = Path(os.environ.get("APPDATA", str(Path.home()))) / "AntigravityTokenMeter"
STATE_DIR = APP_DIR / "state"
LEDGER_PATH = APP_DIR / "task_usage.jsonl"
DEBUG_LOG = APP_DIR / "hook_debug.log"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def log_event(event: str, **fields: Any) -> None:
    """Append metadata-only diagnostics; never log transcript text or secrets."""
    try:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        row = {"time": utc_now(), "event": event, **fields}
        with DEBUG_LOG.open("a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
    except OSError:
        pass


def as_int(value: Any) -> int:
    if isinstance(value, bool) or value is None:
        return 0
    try:
        return max(0, int(value))
    except (TypeError, ValueError, OverflowError):
        return 0


def state_path(conversation_id: str) -> Path:
    safe_id = "".join(c for c in conversation_id if c.isalnum() or c in "-_")
    if not safe_id:
        raise ValueError("Invalid or missing conversationId")
    return STATE_DIR / f"{safe_id}.json"


def load_state(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=path.stem + "_", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            json.dump(state, f, ensure_ascii=False, separators=(",", ":"))
            f.write("\n")
        os.replace(temp_name, path)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def append_ledger(row: dict[str, Any]) -> None:
    LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    with LEDGER_PATH.open("ab") as f:
        f.write(encoded)
        f.flush()
        os.fsync(f.fileno())


def last_user_input_offset(transcript: Path) -> int:
    """Return byte offset of the latest USER_INPUT line, or EOF if none.

    SessionStart can occur after the initial prompt has already been written.
    Starting at the latest USER_INPUT includes that prompt while avoiding older
    conversation history when a conversation is resumed without existing state.
    """
    last_offset: int | None = None
    current_offset = 0
    try:
        with transcript.open("rb") as f:
            for raw in f:
                next_offset = current_offset + len(raw)
                if not raw.endswith(b"\n"):
                    # Ignore an incomplete final JSONL record.
                    break
                try:
                    record = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    current_offset = next_offset
                    continue
                if isinstance(record, dict) and record.get("type") == "USER_INPUT":
                    last_offset = current_offset
                current_offset = next_offset
    except OSError:
        return 0
    return last_offset if last_offset is not None else current_offset


def empty_task(task_no: int, record: dict[str, Any]) -> dict[str, Any]:
    return {
        "task_no": task_no,
        "started_at": record.get("created_at") or record.get("timestamp"),
        "planner_responses": 0,
        "uncached_input_tokens": 0,
        "cache_read_tokens": 0,
        "output_tokens": 0,
        "models": [],
    }


def finalize_task(
    task: dict[str, Any],
    conversation_id: str,
    hook_payload: dict[str, Any],
    status: str,
) -> dict[str, Any]:
    uncached = as_int(task.get("uncached_input_tokens"))
    cached = as_int(task.get("cache_read_tokens"))
    output = as_int(task.get("output_tokens"))
    models = list(dict.fromkeys(task.get("models", [])))
    return {
        "schema_version": 2,
        "source": "antigravity_transcript",
        "task_id": f"{conversation_id}:{task['task_no']}",
        "conversation_id": conversation_id,
        "task_no": task["task_no"],
        "started_at": task.get("started_at"),
        "finished_at": utc_now(),
        "workspace_paths": hook_payload.get("workspacePaths", []),
        "model_names": models,
        "model_name_at_stop": hook_payload.get("modelName"),
        "planner_responses": as_int(task.get("planner_responses")),
        "input_uncached_tokens": uncached,
        "input_cache_read_tokens": cached,
        "input_tokens_total": uncached + cached,
        "output_tokens": output,
        "total_tokens": uncached + cached + output,
        "status": status,
        "termination_reason": hook_payload.get("terminationReason"),
        "usage_note": "input_tokens_total = uncached input + cache read; cache is shown separately and should not be priced as ordinary input.",
    }


def initialize_session(payload: dict[str, Any]) -> dict[str, Any]:
    """Initialize a new conversation without importing previous task history."""
    conversation_id = str(payload.get("conversationId") or "")
    transcript_value = payload.get("transcriptPath")
    if not conversation_id or not transcript_value:
        log_event("session_start_missing_fields", has_conversation_id=bool(conversation_id), has_transcript_path=bool(transcript_value))
        print("Token meter: SessionStart payload missing conversationId or transcriptPath", file=sys.stderr)
        return {"injectSteps": []}

    transcript = Path(str(transcript_value)).expanduser()
    sp = state_path(conversation_id)
    existing = load_state(sp)

    # Do not reset a live offset if SessionStart is re-fired for an existing
    # conversation. Resetting would cause duplicate historical accounting.
    if existing and existing.get("transcript_path") == str(transcript):
        log_event("session_start_existing_state_preserved", conversation_id=conversation_id, byte_offset=as_int(existing.get("byte_offset")))
        return {"injectSteps": []}

    if transcript.is_file():
        offset = last_user_input_offset(transcript)
        size = transcript.stat().st_size
    else:
        # SessionStart may precede physical creation of transcript.jsonl.
        # Keep offset zero; Stop will read it once created.
        offset = 0
        size = 0

    state = {
        "conversation_id": conversation_id,
        "transcript_path": str(transcript),
        "byte_offset": offset,
        "next_task_no": 1,
        "current_task": None,
        "created_at": utc_now(),
        "updated_at": utc_now(),
        "initial_transcript_bytes": size,
        "note": "Initialized by SessionStart. Existing history before the latest USER_INPUT is excluded.",
    }
    save_state(sp, state)
    log_event("session_start_initialized", conversation_id=conversation_id, transcript_path=str(transcript), byte_offset=offset, transcript_bytes=size)
    print(f"Token meter: initialized conversation {conversation_id} at byte {offset}", file=sys.stderr)
    return {"injectSteps": []}


def process_hook(payload: dict[str, Any]) -> dict[str, Any]:
    conversation_id = str(payload.get("conversationId") or "")
    transcript_value = payload.get("transcriptPath")
    if not conversation_id or not transcript_value:
        log_event("stop_missing_fields", has_conversation_id=bool(conversation_id), has_transcript_path=bool(transcript_value))
        print("Token meter: Stop payload missing conversationId or transcriptPath", file=sys.stderr)
        return {"decision": "stop"}

    transcript = Path(str(transcript_value)).expanduser()
    if not transcript.is_file():
        log_event("stop_transcript_missing", conversation_id=conversation_id, transcript_path=str(transcript))
        print(f"Token meter: transcript not found: {transcript}", file=sys.stderr)
        return {"decision": "stop"}

    sp = state_path(conversation_id)
    state = load_state(sp)
    if state is None:
        # Do not silently import the whole old transcript. A missing state must
        # be diagnosed rather than generating misleading task usage.
        log_event("stop_missing_state", conversation_id=conversation_id, transcript_path=str(transcript))
        print(f"Token meter: no state for conversation {conversation_id}; SessionStart was not observed.", file=sys.stderr)
        return {"decision": "stop"}

    if state.get("transcript_path") != str(transcript):
        log_event("stop_transcript_path_mismatch", conversation_id=conversation_id, expected=state.get("transcript_path"), received=str(transcript))
        print("Token meter: transcript path changed for an existing conversation state", file=sys.stderr)
        return {"decision": "stop"}

    file_size = transcript.stat().st_size
    offset = as_int(state.get("byte_offset"))
    if offset > file_size:
        log_event("transcript_shrank", conversation_id=conversation_id, old_offset=offset, file_size=file_size)
        state["byte_offset"] = file_size
        state["current_task"] = None
        state["updated_at"] = utc_now()
        save_state(sp, state)
        return {"decision": "stop"}

    with transcript.open("rb") as f:
        f.seek(offset)
        chunk = f.read()

    last_newline = chunk.rfind(b"\n")
    if last_newline < 0:
        complete_bytes = b""
        new_offset = offset
    else:
        complete_bytes = chunk[: last_newline + 1]
        new_offset = offset + len(complete_bytes)

    task = state.get("current_task")
    task_no = as_int(state.get("next_task_no")) or 1
    completed = 0
    unassigned_responses = 0

    for raw_line in complete_bytes.splitlines():
        if not raw_line.strip():
            continue
        try:
            record = json.loads(raw_line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(record, dict):
            continue

        event_type = record.get("type")
        if event_type == "USER_INPUT":
            if task is not None:
                append_ledger(finalize_task(task, conversation_id, payload, "superseded"))
                completed += 1
                task_no += 1
            task = empty_task(task_no, record)
        elif event_type == "PLANNER_RESPONSE":
            if task is None:
                unassigned_responses += 1
                continue
            task["planner_responses"] = as_int(task.get("planner_responses")) + 1
            task["uncached_input_tokens"] = as_int(task.get("uncached_input_tokens")) + as_int(record.get("input_tokens"))
            task["cache_read_tokens"] = as_int(task.get("cache_read_tokens")) + as_int(record.get("cache_read_tokens"))
            task["output_tokens"] = as_int(task.get("output_tokens")) + as_int(record.get("output_tokens"))
            model_name = record.get("modelName") or record.get("model_name")
            if model_name:
                task.setdefault("models", []).append(str(model_name))

    fully_idle = payload.get("fullyIdle") is True
    has_incomplete_tail = new_offset < file_size
    # If the JSONL file has a partial trailing record, wait for a later Stop
    # rather than finalizing before that record can be counted.
    if fully_idle and task is not None and not has_incomplete_tail:
        append_ledger(finalize_task(task, conversation_id, payload, "completed"))
        completed += 1
        task = None
        task_no += 1

    processed_bytes = new_offset - offset
    state.update({
        "conversation_id": conversation_id,
        "transcript_path": str(transcript),
        "byte_offset": new_offset,
        "next_task_no": task_no,
        "current_task": task,
        "updated_at": utc_now(),
        "last_fully_idle": fully_idle,
    })
    save_state(sp, state)
    log_event(
        "stop_processed",
        conversation_id=conversation_id,
        processed_bytes=processed_bytes,
        file_size=file_size,
        byte_offset=new_offset,
        fully_idle=fully_idle,
        has_incomplete_tail=has_incomplete_tail,
        finalized_tasks=completed,
        unassigned_planner_responses=unassigned_responses,
        pending_task=(task is not None),
        termination_reason=payload.get("terminationReason"),
    )
    print(f"Token meter: processed {processed_bytes} bytes; finalized {completed} task(s).", file=sys.stderr)
    # Per Antigravity docs, any value other than "continue" permits stopping.
    return {"decision": "stop"}


def bootstrap(transcript_value: str) -> None:
    transcript = Path(transcript_value).expanduser().resolve()
    if not transcript.is_file():
        raise SystemExit(f"Transcript does not exist: {transcript}")
    try:
        conversation_id = transcript.parents[2].name
    except IndexError as exc:
        raise SystemExit("Unexpected transcript path layout") from exc
    sp = state_path(conversation_id)
    existing = load_state(sp)
    if existing:
        print(f"State already exists for {conversation_id}; refusing to overwrite it.")
        print(f"State file: {sp}")
        return
    state = {
        "conversation_id": conversation_id,
        "transcript_path": str(transcript),
        "byte_offset": transcript.stat().st_size,
        "next_task_no": 1,
        "current_task": None,
        "created_at": utc_now(),
        "updated_at": utc_now(),
        "note": "Live tracking starts after this byte offset; older history is intentionally not imported.",
    }
    save_state(sp, state)
    log_event("manual_bootstrap", conversation_id=conversation_id, byte_offset=state["byte_offset"], transcript_path=str(transcript))
    print(f"Bootstrapped conversation: {conversation_id}")
    print(f"Transcript bytes skipped: {state['byte_offset']}")
    print(f"State file: {sp}")
    print(f"Ledger: {LEDGER_PATH}")


def show_report(limit: int = 20) -> None:
    if not LEDGER_PATH.exists():
        print("No usage records yet.")
        print(f"Debug log: {DEBUG_LOG}")
        return
    rows: list[dict[str, Any]] = []
    with LEDGER_PATH.open("r", encoding="utf-8") as f:
        for line in f:
            try:
                row = json.loads(line)
                if isinstance(row, dict):
                    rows.append(row)
            except json.JSONDecodeError:
                continue
    for row in rows[-limit:]:
        print(json.dumps(row, ensure_ascii=False, indent=2))
    print("\nLedger totals (Antigravity only):")
    print(json.dumps({
        "tasks": len(rows),
        "planner_responses": sum(as_int(x.get("planner_responses")) for x in rows),
        "input_uncached_tokens": sum(as_int(x.get("input_uncached_tokens")) for x in rows),
        "input_cache_read_tokens": sum(as_int(x.get("input_cache_read_tokens")) for x in rows),
        "input_tokens_total": sum(as_int(x.get("input_tokens_total")) for x in rows),
        "output_tokens": sum(as_int(x.get("output_tokens")) for x in rows),
        "total_tokens": sum(as_int(x.get("total_tokens")) for x in rows),
    }, ensure_ascii=False, indent=2))
    print(f"\nDebug log: {DEBUG_LOG}")


def show_diagnostics(limit: int = 30) -> None:
    print(f"APP_DIR: {APP_DIR}")
    print(f"STATE_DIR: {STATE_DIR}")
    print(f"LEDGER_PATH: {LEDGER_PATH}")
    print(f"DEBUG_LOG: {DEBUG_LOG}")
    if not DEBUG_LOG.exists():
        print("No hook events logged yet.")
        return
    print("\nRecent hook events:")
    lines = DEBUG_LOG.read_text(encoding="utf-8", errors="replace").splitlines()
    for line in lines[-limit:]:
        print(line)


def main() -> int:
    parser = argparse.ArgumentParser(description="Antigravity transcript Token meter (no AI/network calls)")
    parser.add_argument("--bootstrap", metavar="TRANSCRIPT_PATH", help="Start live tracking at current EOF; does not import old history")
    parser.add_argument("--session-start", action="store_true", help="Initialize a conversation from SessionStart hook stdin")
    parser.add_argument("--stop", action="store_true", help="Process appended transcript records from Stop hook stdin")
    parser.add_argument("--report", action="store_true", help="Print local JSONL ledger totals")
    parser.add_argument("--diagnostics", action="store_true", help="Show recent hook event diagnostics (no transcript content)")
    args = parser.parse_args()

    if args.bootstrap:
        bootstrap(args.bootstrap)
        return 0
    if args.report:
        show_report()
        return 0
    if args.diagnostics:
        show_diagnostics()
        return 0

    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            raise ValueError("Hook stdin must be a JSON object")
        if args.session_start:
            response = initialize_session(payload)
        elif args.stop:
            response = process_hook(payload)
        else:
            raise ValueError("Specify --session-start, --stop, --bootstrap, --report, or --diagnostics")
        sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
        sys.stdout.flush()
        return 0
    except Exception as exc:  # Accounting must never block Antigravity.
        log_event("hook_exception", error_type=type(exc).__name__, error=str(exc)[:500], mode="session_start" if args.session_start else "stop")
        print(f"Token meter error: {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.stdout.write('{"decision":"stop"}\n')
        sys.stdout.flush()
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
