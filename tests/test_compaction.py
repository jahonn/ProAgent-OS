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


def _tool_boundary_messages():
    msgs = [{"role": "system", "content": "sys"}]
    for i in range(15):
        msgs.append({"role": "user", "content": f"u{i}"})
        msgs.append({"role": "assistant", "content": f"a{i}"})
    msgs.append({"role": "assistant", "content": "calling"})
    msgs.append({"role": "tool", "content": '{"ok": true, "output": "recent tool"}'})
    return msgs


def _recent(result):
    return [m for m in result.messages if m["role"] not in {"system", SUMMARY_ROLE}]


def test_default_summarizer_setting_is_extractive(monkeypatch):
    monkeypatch.delenv("PROAGENT_COMPACTION_SUMMARIZER", raising=False)
    from proagent_os.config import get_settings

    get_settings.cache_clear()
    assert get_settings().compaction_summarizer == "extractive"
    msgs = _tool_boundary_messages()
    cfg = CompactionConfig(max_messages=5, keep_recent=1, max_chars=50_000)
    result = compact_messages(msgs, cfg)
    assert result.compacted is True
    assert result.summary_text.startswith("Earlier turns:")
    assert result.meta["summarizer"] == "ExtractiveSummarizer"
    assert "summarizer_mode" not in result.meta
    assert "summarizer_fallback" not in result.meta
    assert "Mock compaction summary" not in result.summary_text


def test_llm_mode_mock_provider_keeps_last_n_and_tool_pair(monkeypatch):
    """llm + mock yields a deterministic summary and the same keep/tool window."""
    monkeypatch.setenv("PROAGENT_COMPACTION_SUMMARIZER", "llm")
    monkeypatch.setenv("PROAGENT_LLM_PROVIDER", "mock")
    from proagent_os.config import get_settings
    from proagent_os.harness.compaction import LlmSummarizer, summarizer_for_settings
    from proagent_os.llm.factory import reset_llm_cache
    from proagent_os.llm.mock import MOCK_COMPACTION_SUMMARY

    get_settings.cache_clear()
    reset_llm_cache()
    msgs = _tool_boundary_messages()
    # keep_recent=1 lands the cut on the trailing tool row; the pair rule
    # pulls the assistant tool call back into the raw window.
    cfg = CompactionConfig(max_messages=5, keep_recent=1, max_chars=50_000, summary_max_chars=500)
    baseline = compact_messages(msgs, cfg)
    chosen = summarizer_for_settings(get_settings(), max_chars=cfg.summary_max_chars)
    assert isinstance(chosen, LlmSummarizer)
    result = compact_messages(msgs, cfg, chosen)

    assert result.compacted is True
    assert result.summary_text == MOCK_COMPACTION_SUMMARY
    assert result.meta["summarizer"] == "LlmSummarizer"
    assert result.meta["summarizer_mode"] == "llm"
    assert "summarizer_fallback" not in result.meta
    assert result.dropped_count == baseline.dropped_count
    assert result.meta["kept_recent"] == baseline.meta["kept_recent"]
    assert [m["role"] for m in result.messages] == [m["role"] for m in baseline.messages]
    recent = _recent(result)
    assert recent[-1]["role"] == "tool"
    assert "recent tool" in recent[-1]["content"]
    assert recent[-2]["role"] == "assistant"
    assert recent[-2]["content"] == "calling"
    assert all("u0" not in str(m.get("content")) for m in recent)
    assert len(recent) >= 2


class _RecordingProvider:
    def __init__(self, content: str) -> None:
        self.content = content
        self.calls = []

    async def chat(self, messages, **kwargs):
        from proagent_os.llm.base import LLMResponse

        self.calls.append({"messages": messages, "kwargs": kwargs})
        return LLMResponse(content=self.content, model="recording")


class _BoomProvider:
    async def chat(self, messages, **kwargs):
        raise RuntimeError("compaction model unavailable")


class _EmptyProvider:
    async def chat(self, messages, **kwargs):
        from proagent_os.llm.base import LLMResponse

        return LLMResponse(content="  ", model="empty")


def test_llm_summarizer_calls_chat_client_without_tools():
    from proagent_os.harness.compaction import COMPACTION_SUMMARY_SYSTEM, LlmSummarizer

    provider = _RecordingProvider("Injected summary of older turns.")
    summarizer = LlmSummarizer(provider=provider, max_chars=400)
    text = summarizer.summarize([{"role": "user", "content": "old turn about billing"}])
    assert text == "Injected summary of older turns."
    assert summarizer.last_mode == "llm"
    assert summarizer.last_fallback is None
    assert len(provider.calls) == 1
    messages = provider.calls[0]["messages"]
    assert messages[0].role == "system"
    assert messages[0].content == COMPACTION_SUMMARY_SYSTEM
    assert messages[1].role == "user"
    assert "old turn about billing" in messages[1].content
    assert provider.calls[0]["kwargs"]["tools"] is None
    assert provider.calls[0]["kwargs"]["tool_choice"] is None


def test_llm_summarizer_falls_back_to_extractive_and_records_meta():
    from proagent_os.harness.compaction import LlmSummarizer

    msgs = _tool_boundary_messages()
    cfg = CompactionConfig(max_messages=5, keep_recent=1, max_chars=50_000, summary_max_chars=800)
    baseline = compact_messages(msgs, cfg)
    result = compact_messages(msgs, cfg, LlmSummarizer(provider=_BoomProvider(), max_chars=800))
    assert result.summary_text.startswith("Earlier turns:")
    assert result.summary_text == baseline.summary_text
    assert result.meta["summarizer_mode"] == "extractive_fallback"
    assert "RuntimeError" in result.meta["summarizer_fallback"]
    assert "compaction model unavailable" in result.meta["summarizer_fallback"]
    assert [m["role"] for m in result.messages] == [m["role"] for m in baseline.messages]
    assert _recent(result)[-1]["role"] == "tool"

    empty = compact_messages(msgs, cfg, LlmSummarizer(provider=_EmptyProvider(), max_chars=800))
    assert empty.summary_text.startswith("Earlier turns:")
    assert empty.meta["summarizer_mode"] == "extractive_fallback"
    assert "empty compaction summary" in empty.meta["summarizer_fallback"]


def test_conversation_memory_llm_mode_persists_mock_summary(monkeypatch):
    monkeypatch.setenv("PROAGENT_COMPACTION_SUMMARIZER", "llm")
    from proagent_os.config import get_settings
    from proagent_os.llm.factory import reset_llm_cache
    from proagent_os.llm.mock import MOCK_COMPACTION_SUMMARY

    get_settings.cache_clear()
    reset_llm_cache()
    tid = new_id("ten")
    get_db().insert(
        "tenants",
        {
            "id": tid,
            "name": "LLM Mem",
            "api_key": "pk_llm_mem",
            "goals": "",
            "constraints": "",
            "tone": "professional",
            "created_at": utcnow(),
        },
    )
    mem = ConversationMemory(
        compaction_config=CompactionConfig(
            enabled=True, max_messages=4, keep_recent=2, max_chars=50_000, summary_max_chars=500
        )
    )
    cid = mem.create_conversation(tid, title="llm")
    for i in range(8):
        mem.add_message(cid, "user", f"hello {i}")
        mem.add_message(cid, "assistant", f"world {i}")
    result = mem.compact_if_needed(cid, tenant_id=tid, persist=True)
    assert result.compacted is True
    assert result.summary_text == MOCK_COMPACTION_SUMMARY
    rows = mem.history_all(cid)
    summaries = [r for r in rows if r["role"] == SUMMARY_ROLE]
    assert summaries[-1]["content"] == MOCK_COMPACTION_SUMMARY
    meta = loads(summaries[-1]["meta_json"] or "{}")
    assert meta.get("summarizer_mode") == "llm"
    assert "summarizer_fallback" not in meta
    # Originals are still stored; the raw tail is still the newest turns.
    assert rows[-2]["content"] == "world 7" or any(r["content"] == "world 7" for r in rows)


@pytest.mark.asyncio
async def test_orchestrator_llm_compaction_trace_uses_mock_summary(monkeypatch):
    monkeypatch.setenv("PROAGENT_COMPACTION_SUMMARIZER", "llm")
    monkeypatch.setenv("PROAGENT_COMPACTION_MAX_MESSAGES", "4")
    monkeypatch.setenv("PROAGENT_COMPACTION_KEEP_RECENT", "2")
    from proagent_os.config import get_settings
    from proagent_os.llm.factory import reset_llm_cache
    from proagent_os.llm.mock import MOCK_COMPACTION_SUMMARY

    get_settings.cache_clear()
    reset_llm_cache()
    tid = new_id("ten")
    get_db().insert(
        "tenants",
        {
            "id": tid,
            "name": "LLM Trace",
            "api_key": "pk_llm_trace",
            "goals": "",
            "constraints": "",
            "tone": "professional",
            "created_at": utcnow(),
        },
    )
    svc = get_services()
    cid = svc.orchestrator.memory.create_conversation(tid, title="seed-llm")
    for i in range(6):
        svc.orchestrator.memory.add_message(cid, "user", f"seed user {i}")
        svc.orchestrator.memory.add_message(cid, "assistant", f"seed asst {i}")
    svc.kb.ingest_text(tid, "manual", "Note", "Conversion is soft this week.")
    result = await svc.orchestrator.run_task(
        tid, "What should we do about conversion?", conversation_id=cid
    )
    assert result["status"] == "completed"
    compaction_events = [t for t in result["trace"] if t.get("phase") == LoopPhase.COMPACTION.value]
    assert compaction_events
    assert compaction_events[0].get("summarizer_mode") == "llm"
    assert "summarizer_fallback" not in compaction_events[0]
    summaries = [
        r for r in svc.orchestrator.memory.history_all(cid) if r["role"] == SUMMARY_ROLE
    ]
    assert summaries
    assert summaries[-1]["content"] == MOCK_COMPACTION_SUMMARY


@pytest.mark.asyncio
async def test_orchestrator_records_extractive_fallback_on_trace():
    from proagent_os.harness.compaction import LlmSummarizer

    tid = new_id("ten")
    get_db().insert(
        "tenants",
        {
            "id": tid,
            "name": "Fallback Trace",
            "api_key": "pk_fb_trace",
            "goals": "",
            "constraints": "",
            "tone": "professional",
            "created_at": utcnow(),
        },
    )
    svc = get_services()
    svc.orchestrator.memory.summarizer = LlmSummarizer(provider=_BoomProvider(), max_chars=800)
    svc.orchestrator.memory._compaction_config = CompactionConfig(
        enabled=True, max_messages=4, keep_recent=2, max_chars=50_000, summary_max_chars=800
    )
    cid = svc.orchestrator.memory.create_conversation(tid, title="seed-fb")
    for i in range(8):
        svc.orchestrator.memory.add_message(cid, "user", f"seed user {i}")
        svc.orchestrator.memory.add_message(cid, "assistant", f"seed asst {i}")
    result = await svc.orchestrator.run_task(tid, "Check the notes", conversation_id=cid)
    assert result["status"] == "completed"
    compaction_events = [t for t in result["trace"] if t.get("phase") == LoopPhase.COMPACTION.value]
    assert compaction_events
    assert compaction_events[0].get("summarizer_mode") == "extractive_fallback"
    assert "RuntimeError" in str(compaction_events[0].get("summarizer_fallback"))
    summaries = [
        r for r in svc.orchestrator.memory.history_all(cid) if r["role"] == SUMMARY_ROLE
    ]
    assert summaries[-1]["content"].startswith("Earlier turns:")
