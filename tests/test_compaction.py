"""Unit tests for session / conversation compaction stub."""

from __future__ import annotations

import pytest

from proagent_os.db import get_db, loads, new_id, utcnow
from proagent_os.harness.compaction import (
    SUMMARY_ROLE,
    CompactionConfig,
    ExtractiveSummarizer,
    approx_chars,
    compact_messages,
    messages_for_llm,
)
from proagent_os.harness.loop import LoopPhase
from proagent_os.harness.memory import ConversationMemory
from proagent_os.services import get_services


def _msgs(n: int, *, prefix: str = "turn"):
    out = [{"role": "system", "content": "You are helpful."}]
    for i in range(n):
        out.append({"role": "user", "content": f"{prefix} user {i} " + ("x" * 20)})
        out.append({"role": "assistant", "content": f"{prefix} assistant {i}"})
    return out


def test_compact_under_threshold_noop():
    msgs = _msgs(3)
    result = compact_messages(
        msgs, CompactionConfig(max_messages=40, keep_recent=12, max_chars=50_000)
    )
    assert result.compacted is False
    assert result.reason == "under_threshold"
    assert len(result.messages) == len(msgs)


def test_compact_keep_system_and_last_n():
    msgs = _msgs(20)  # 1 system + 40 non-system
    cfg = CompactionConfig(max_messages=10, keep_recent=6, max_chars=50_000)
    result = compact_messages(msgs, cfg, ExtractiveSummarizer(max_chars=800))
    assert result.compacted is True
    assert result.dropped_count > 0
    roles = [m["role"] for m in result.messages]
    assert roles[0] == "system"
    assert SUMMARY_ROLE in roles
    # Recent window is last 6 non-system (may grow slightly for tool pairs).
    non_sys = [m for m in result.messages if m["role"] not in {"system", SUMMARY_ROLE}]
    assert len(non_sys) <= 8
    assert len(non_sys) >= 6
    assert "user 19" in non_sys[-2]["content"] or "user 19" in non_sys[-1]["content"]


def test_messages_for_llm_maps_summary_to_system():
    wire = messages_for_llm(
        [
            {"role": "system", "content": "base"},
            {"role": SUMMARY_ROLE, "content": "older stuff"},
            {"role": "user", "content": "hi"},
        ]
    )
    assert wire[1]["role"] == "system"
    assert wire[1]["content"].startswith("[Conversation summary]")
    assert wire[2]["role"] == "user"


def test_tool_pair_boundary_prefers_not_to_split():
    msgs = [{"role": "system", "content": "sys"}]
    for i in range(15):
        msgs.append({"role": "user", "content": f"u{i}"})
        msgs.append({"role": "assistant", "content": f"a{i}"})
    # End with a tool result that would sit at the cut if keep_recent is tight.
    msgs.append({"role": "assistant", "content": "calling"})
    msgs.append({"role": "tool", "content": '{"ok": true, "output": "recent tool"}'})
    cfg = CompactionConfig(max_messages=5, keep_recent=2, max_chars=50_000)
    result = compact_messages(msgs, cfg)
    assert result.compacted is True
    recent = [m for m in result.messages if m["role"] not in {"system", SUMMARY_ROLE}]
    assert any(m["role"] == "tool" for m in recent)
    assert "recent tool" in result.summary_text or any(
        "recent tool" in str(m.get("content")) for m in recent
    )


def test_disabled_config():
    msgs = _msgs(30)
    result = compact_messages(msgs, CompactionConfig(enabled=False, max_messages=1))
    assert result.compacted is False
    assert result.reason == "disabled"


def test_approx_chars_positive():
    assert approx_chars([{"role": "user", "content": "abcd"}]) >= 4


def test_conversation_memory_persists_summary_and_tenant_note():
    tid = new_id("ten")
    get_db().insert(
        "tenants",
        {
            "id": tid,
            "name": "Mem Co",
            "api_key": "pk_mem",
            "goals": "",
            "constraints": "",
            "tone": "professional",
            "created_at": utcnow(),
        },
    )
    mem = ConversationMemory(
        compaction_config=CompactionConfig(
            enabled=True, max_messages=6, keep_recent=4, max_chars=50_000
        )
    )
    cid = mem.create_conversation(tid, title="long")
    for i in range(10):
        mem.add_message(cid, "user", f"hello {i}")
        mem.add_message(cid, "assistant", f"world {i}")

    result = mem.compact_if_needed(cid, tenant_id=tid, persist=True)
    assert result.compacted is True
    rows = mem.history_all(cid)
    summaries = [r for r in rows if r["role"] == SUMMARY_ROLE]
    assert len(summaries) >= 1
    meta = loads(summaries[-1]["meta_json"] or "{}")
    assert meta.get("compaction") is True
    notes = mem.list_tenant_memory(tid, limit=5)
    assert any(n["kind"] == "compaction" for n in notes)

    wire = mem.history_for_llm(cid)
    assert any(
        m["role"] == "system" and "[Conversation summary]" in str(m.get("content") or "")
        for m in wire
    )


@pytest.mark.asyncio
async def test_orchestrator_compaction_trace_on_long_conversation():
    tid = new_id("ten")
    get_db().insert(
        "tenants",
        {
            "id": tid,
            "name": "Trace Co",
            "api_key": "pk_trace",
            "goals": "",
            "constraints": "",
            "tone": "professional",
            "created_at": utcnow(),
        },
    )
    svc = get_services()
    # Force low thresholds for this test process.
    from proagent_os.config import get_settings

    get_settings.cache_clear()
    import os

    os.environ["PROAGENT_COMPACTION_MAX_MESSAGES"] = "4"
    os.environ["PROAGENT_COMPACTION_KEEP_RECENT"] = "2"
    get_settings.cache_clear()

    mem = ConversationMemory()
    # Rebuild orchestrator memory config by using existing services memory
    # and compacting via env-backed settings on the next run_task.
    cid = svc.orchestrator.memory.create_conversation(tid, title="seed")
    for i in range(6):
        svc.orchestrator.memory.add_message(cid, "user", f"seed user {i}")
        svc.orchestrator.memory.add_message(cid, "assistant", f"seed asst {i}")

    svc.kb.ingest_text(tid, "manual", "Note", "Conversion is soft this week.")
    result = await svc.orchestrator.run_task(
        tid, "What should we do about conversion?", conversation_id=cid
    )
    assert result["status"] == "completed"
    assert any(t.get("phase") == LoopPhase.COMPACTION.value for t in result["trace"])
