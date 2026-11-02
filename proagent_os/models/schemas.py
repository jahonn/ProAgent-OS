from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field


class TenantCreate(BaseModel):
    name: str
    goals: str = ""
    constraints: str = ""
    tone: str = "professional"


class TenantUpdate(BaseModel):
    goals: Optional[str] = None
    constraints: Optional[str] = None
    tone: Optional[str] = None


class TenantOut(BaseModel):
    id: str
    name: str
    api_key: str
    goals: str = ""
    constraints: str = ""
    tone: str = "professional"
    created_at: str
    key_status: str = "active"


class ApiKeyRotateOut(BaseModel):
    tenant_id: str
    api_key: str
    prefix: str
    previous_prefix: Optional[str] = None
    key_status: Literal["active"] = "active"
    rotated_at: str
    detail: str


class TenantRoleUpdate(BaseModel):
    role: Literal["tenant_operator", "tenant_readonly"]


class ApiKeyRevokeOut(BaseModel):
    tenant_id: str
    revoked: bool = True
    already_revoked: bool = False
    prefix: str = ""
    key_status: Literal["revoked"] = "revoked"
    revoked_at: str
    recovery: str


class TaskCreate(BaseModel):
    input: str
    conversation_id: Optional[str] = None


class KbIngestUrl(BaseModel):
    url: str


class KbIngestText(BaseModel):
    title: str
    content: str
    source: str = "manual"


class KbSearchQuery(BaseModel):
    query: str
    top_k: int = 5


class MonitorCreate(BaseModel):
    name: str
    kind: str = Field(description="kb_change | metric_stub | data_source")
    config: Dict[str, Any] = Field(default_factory=dict)
    interval_seconds: int = 60


class RecommendationDecide(BaseModel):
    status: Literal["approved", "rejected", "executed"]


class OutcomeCreate(BaseModel):
    action_id: str
    metric_name: str
    value: float
    meta: Dict[str, Any] = Field(default_factory=dict)
    skill_id: Optional[str] = None
    success_threshold: float = 0.0


class SkillInvoke(BaseModel):
    arguments: Dict[str, Any] = Field(default_factory=dict)


class TaskPause(BaseModel):
    """Explicit pause of a running task (run-level HITL stub)."""

    reason: str = "human_approval_required"
    pending_action: Dict[str, Any] = Field(default_factory=dict)


class TaskHitlNote(BaseModel):
    """Optional note on resume / reject.

    ``reexecute`` applies only to resume, and only while the task is still
    ``awaiting_human``. It is the explicit re-approve for a gated-tool execute
    receipt left in ``started`` (crash mid-flight). Default false: that resume
    fails closed and does not run the side effect again. A ``succeeded`` or
    ``failed`` receipt is never run again, even when this is true. A task
    already ``running`` ignores this field: a finished receipt is recovered
    without another invoke, and a ``started`` receipt stays fail-closed.
    Reject ignores the field and still does not execute.
    """

    note: str = ""
    reexecute: bool = False

