# Writing Your First Vertical Skill

A *vertical skill* is a self-contained, executable capability that the ProAgent planner can invoke. Each skill lives in its own directory under `skills/` and exposes a `run(tenant_id, **kwargs)` function.

---

## 1. Create the Skill Directory

```bash
mkdir -p skills/my-skill
```

---

## 2. Write `SKILL.md` (Required)

This front-matter + markdown file tells the planner what the skill does.

```markdown
---
id: my-skill          # unique, kebab-case, matches directory name
name: My Skill        # human-readable name
description: One-sentence summary for the planner
---

# My Skill

Longer description of what the skill does, when to invoke it, and what it returns.
```

**Fields**
- `id`: Must match the directory name (`skills/my-skill/`).
- `name`: Display name in the planner UI.
- `description`: One sentence; the planner uses this for tool selection.

---

## 3. Write `run.py` (Required)

```python
"""Optional executable skill pack."""
from __future__ import annotations
from typing import Any, Dict


def run(tenant_id: str = "", **kwargs: Any) -> Dict[str, Any]:
    """Execute the skill.

    Args:
        tenant_id: Current tenant identifier.
        **kwargs: Skill-specific parameters (validated by the planner).

    Returns:
        A JSON-serializable dict. Common keys:
        - `title`: Short result title
        - `summary`: Human-readable summary
        - `data`: Structured payload
        - `confidence`: 0.0-1.0 confidence score
    """
    # Your logic here
    return {
        "title": "My Skill Result",
        "summary": "Done.",
        "data": {},
        "confidence": 1.0,
    }
```

**Signature rules**
- First parameter **must** be `tenant_id: str = ""`.
- Accept `**kwargs` for forward-compatibility.
- Return a `Dict[str, Any]` (JSON-serializable).

---

## 4. (Optional) Add Unit Tests

Create `test_run.py` alongside `run.py`:

```python
import pytest
from run import run


def test_my_skill():
    result = run(tenant_id="test-tenant")
    assert "title" in result
    assert isinstance(result["title"], str)
```

Run with:
```bash
pytest skills/my-skill/test_run.py
```

---

## 5. Register the Skill (Automatic)

Skills are auto-discovered from `skills/*/SKILL.md`. No extra registration needed.

Restart the worker or API server to pick up new skills:
```bash
uvicorn proagent_os.main:app --reload
```

---

## 6. Test via the API

```bash
curl -s -X POST http://localhost:8080/api/v1/skills/my-skill/invoke \
  -H "Authorization: Bearer dev-admin-key" \
  -H "Content-Type: application/json" \
  -d '{"tenant_id": "test-tenant"}'
```

---

## 7. Best Practices

- **Keep it small**: One skill = one clear responsibility.
- **Validate inputs** in `run()`; raise `ValueError` for bad args.
- **Return structured data**, not free-form strings.
- **Document side effects** in `SKILL.md` (writes, API calls, etc.).
- **Use the mock provider** for development: `PROAGENT_LLM_PROVIDER=mock`.

---

## 8. Example: Complete Minimal Skill

Directory tree:
```
skills/echo/
├── SKILL.md
└── run.py
```

`skills/echo/SKILL.md`:
```markdown
---
id: echo
name: Echo
description: Returns the input message unchanged
---

# Echo

Simple identity skill for testing the skill pipeline.
```

`skills/echo/run.py`:
```python
"""Echo skill."""
from __future__ import annotations
from typing import Any, Dict


def run(tenant_id: str = "", message: str = "hello", **_: Any) -> Dict[str, Any]:
    return {
        "title": "Echo",
        "summary": message,
        "data": {"message": message},
        "confidence": 1.0,
    }
```

---

## 9. Next Steps

- Read the [Decision Analyst](../skills/analyst/SKILL.md) and [Research Brief](../skills/research/SKILL.md) skills for real examples.
- See `scripts/demo.py` for a full end-to-end flow.
- Join the community loop on [issue #2](https://github.com/jahonn/ProAgent-OS/issues/2).
