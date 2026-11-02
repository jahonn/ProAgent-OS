# Example Vertical Pack

Shows how to extend ProAgent OS without modifying core code:

1. Drop agent definitions in `agents/agents.yaml` (merged at startup).
2. Add skill packs under `skills/<id>/SKILL.md` (+ optional `run.py`).
3. Point `PROAGENT_SKILLS_DIRS` at this folder (already included by default).

This pack is intentionally generic (market scan stub) — replace with your industry pack.
Fuller packs: `verticals/support_desk/` (support policies, triage, follow-up monitor) and `verticals/ops_incident/` (on-call runbooks, incident triage, page approval).
