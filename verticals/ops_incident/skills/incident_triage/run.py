"""Deterministic incident triage for the ops desk demo."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

_CITE_NAME = "proagent_vertical_ops_incident_cite"


def _load_cite() -> Any:
    existing = sys.modules.get(_CITE_NAME)
    if existing is not None:
        return existing
    path = Path(__file__).resolve().parents[2] / "cite.py"
    spec = importlib.util.spec_from_file_location(_CITE_NAME, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[_CITE_NAME] = mod
    spec.loader.exec_module(mod)
    return mod


def _follow_up(needs_human: bool, citations: list[dict[str, Any]]) -> str:
    if not citations:
        return "No runbook passage matched. Ask a human before paging or declaring an incident."
    title = citations[0]["title"]
    if needs_human:
        return f"Human follow-up: confirm {title} before paging or posting an update."
    return f"Page using {title} and quote the matching line. A person can still approve the page."


def run(
    tenant_id: str = "",
    page: str = "",
    ticket: str = "",
    passages: Any = None,
    top_k: int = 2,
    **_: Any,
) -> dict[str, Any]:
    helper = _load_cite()
    text = page or ticket or ""
    chosen = helper.load_fixtures() if passages is None else helper.normalize_passages(passages)
    labels = helper.classify_page(text)
    citations = helper.rank(text, chosen, top_k=top_k) if text else []
    human = helper.needs_human(text, labels["severity"], citations)
    return {
        "tenant_id": tenant_id,
        "severity": labels["severity"],
        "service": labels["service"],
        "needs_human": human,
        "follow_up": _follow_up(human, citations),
        "citations": citations,
        "note": (
            "Keyword triage for the offline demo. It cites bundled runbooks or passages you pass in. "
            "It is not a production classifier and it does not page anyone."
        ),
    }
