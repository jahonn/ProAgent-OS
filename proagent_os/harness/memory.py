from __future__ import annotations

from typing import Any, Dict, List, Optional

from proagent_os.config import get_settings
from proagent_os.db import dumps, get_db, loads, new_id, utcnow
from proagent_os.harness.compaction import (
    SUMMARY_ROLE,
    CompactionConfig,
    CompactionResult,
    Summarizer,
    approx_chars,
    compact_messages,
    config_from_settings,
    messages_for_llm,
)


class ConversationMemory:
    """Tenant conversation store with an optional compaction path.

    Compaction keeps originals in ``messages`` for audit and may append a
    ``summary`` role row plus a ``tenant_memory`` kind=compaction note.
    Use :meth:`history_for_llm` when building model context.
    """

    def __init__(
        self,
        *,
        summarizer: Optional[Summarizer] = None,
        compaction_config: Optional[CompactionConfig] = None,
    ) -> None:
        self.db = get_db()
        self.summarizer = summarizer
        self._compaction_config = compaction_config

    def _config(self) -> CompactionConfig:
        if self._compaction_config is not None:
            return self._compaction_config
        return config_from_settings(get_settings())

    def create_conversation(self, tenant_id: str, title: str = "") -> str:
        cid = new_id("conv")
        self.db.insert(
            "conversations",
            {"id": cid, "tenant_id": tenant_id, "title": title, "created_at": utcnow()},
        )
        return cid

    def add_message(
        self,
        conversation_id: str,
        role: str,
        content: str,
        meta: Optional[Dict[str, Any]] = None,
    ) -> str:
        mid = new_id("msg")
        self.db.insert(
            "messages",
            {
                "id": mid,
                "conversation_id": conversation_id,
                "role": role,
                "content": content,
                "meta_json": dumps(meta or {}),
                "created_at": utcnow(),
            },
        )
        return mid

    def history(self, conversation_id: str, limit: int = 50) -> List[Dict[str, Any]]:
        return self.db.fetchall(
            "SELECT * FROM messages WHERE conversation_id=? ORDER BY created_at ASC LIMIT ?",
            (conversation_id, limit),
        )

    def history_all(self, conversation_id: str) -> List[Dict[str, Any]]:
        """Full ordered history (no LIMIT). Used before compaction decisions."""
        return self.db.fetchall(
            "SELECT * FROM messages WHERE conversation_id=? ORDER BY created_at ASC",
            (conversation_id,),
        )

    def append_tenant_memory(self, tenant_id: str, kind: str, content: str) -> str:
        mid = new_id("mem")
        self.db.insert(
            "tenant_memory",
            {
                "id": mid,
                "tenant_id": tenant_id,
                "kind": kind,
                "content": content,
                "created_at": utcnow(),
            },
        )
        return mid

    def list_tenant_memory(self, tenant_id: str, limit: int = 20) -> List[Dict[str, Any]]:
        return self.db.fetchall(
            "SELECT * FROM tenant_memory WHERE tenant_id=? ORDER BY created_at DESC LIMIT ?",
            (tenant_id, limit),
        )

    def compact_if_needed(
        self,
        conversation_id: str,
        *,
        tenant_id: Optional[str] = None,
        persist: bool = True,
    ) -> CompactionResult:
        """Compact conversation history when over thresholds.

        Original message rows are **kept**. When ``persist`` and compaction
        fires, a ``summary`` message is appended (auditable meta) and an
        optional ``tenant_memory`` kind=compaction note is written.
        """
        rows = self.history_all(conversation_id)
        as_maps: List[Dict[str, Any]] = []
        for row in rows:
            meta = loads(row.get("meta_json") or "{}")
            as_maps.append(
                {
                    "id": row.get("id"),
                    "role": row.get("role"),
                    "content": row.get("content") or "",
                    "meta": meta if isinstance(meta, dict) else {},
                    "created_at": row.get("created_at"),
                }
            )

        result = compact_messages(as_maps, config=self._config(), summarizer=self.summarizer)
        if not result.compacted or not persist:
            return result

        # Avoid stacking duplicate summaries every call: if the latest message
        # is already a compaction summary covering the same dropped count, skip.
        if rows:
            last = rows[-1]
            last_meta = loads(last.get("meta_json") or "{}")
            if (
                last.get("role") == SUMMARY_ROLE
                and isinstance(last_meta, dict)
                and last_meta.get("compaction")
                and int(last_meta.get("dropped_count") or 0) == result.dropped_count
                and (last.get("content") or "") == result.summary_text
            ):
                result.reason = "already_persisted"
                return result

        meta = dict(result.meta)
        meta["conversation_id"] = conversation_id
        meta["source_message_ids"] = [
            m.get("id") for m in as_maps if m.get("id") and m.get("role") != SUMMARY_ROLE
        ][: result.dropped_count]
        self.add_message(conversation_id, SUMMARY_ROLE, result.summary_text, meta=meta)

        if tenant_id:
            note = (
                f"compaction conversation_id={conversation_id} "
                f"dropped={result.dropped_count} "
                f"approx_chars_before={meta.get('approx_chars_before')} "
                f"kept_recent={meta.get('kept_recent')}"
            )
            self.append_tenant_memory(tenant_id, kind="compaction", content=note)

        return result

    def history_for_llm(
        self,
        conversation_id: str,
        *,
        tenant_id: Optional[str] = None,
        persist: bool = False,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Return chat-wire messages after optional compaction.

        ``persist=False`` (default) computes a view without writing. Call
        :meth:`compact_if_needed` with persist=True when the orchestrator
        wants an auditable summary row.
        """
        if persist:
            self.compact_if_needed(conversation_id, tenant_id=tenant_id, persist=True)

        cfg = self._config()
        rows = self.history_all(conversation_id)
        as_maps = [
            {
                "role": r.get("role"),
                "content": r.get("content") or "",
                "meta": loads(r.get("meta_json") or "{}"),
            }
            for r in rows
        ]
        # If a persisted summary exists, prefer the view that folds older turns
        # via compact_messages (idempotent when already under threshold).
        result = compact_messages(as_maps, config=cfg, summarizer=self.summarizer)
        wire = messages_for_llm(result.messages)
        if limit is not None and limit > 0:
            return wire[-limit:]
        return wire

    def prior_context_block(
        self,
        conversation_id: str,
        *,
        max_chars: int = 4000,
    ) -> str:
        """Short text block of compacted prior turns for planner/specialist prompts."""
        wire = self.history_for_llm(conversation_id, persist=False)
        if not wire:
            return ""
        # Skip pure system identity prompts that are not summaries; keep summary + dialog.
        lines: List[str] = []
        for msg in wire:
            role = str(msg.get("role") or "")
            content = str(msg.get("content") or "").strip()
            if not content:
                continue
            if role == "system" and not content.startswith("[Conversation summary]"):
                continue
            lines.append(f"{role}: {content}")
        text = "\n".join(lines)
        if len(text) > max_chars:
            return text[-max_chars:]
        return text


# Re-export helpers for callers / tests.
__all__ = [
    "ConversationMemory",
    "approx_chars",
    "compact_messages",
    "messages_for_llm",
]
