"""Quote an on-call runbook passage without calling a model."""
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


def run(
    tenant_id: str = "",
    query: str = "",
    passages: Any = None,
    top_k: int = 3,
    **_: Any,
) -> dict[str, Any]:
    helper = _load_cite()
    chosen = helper.load_fixtures() if passages is None else helper.normalize_passages(passages)
    citations = helper.rank(query, chosen, top_k=top_k) if query else []
    return {
        "tenant_id": tenant_id,
        "query": query,
        "citations": citations,
        "note": (
            "Quotes are copied from the matched passage. "
            "An empty citations list means this pack does not cover the page."
        ),
    }
