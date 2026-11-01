from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import List, Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PROAGENT_", env_file=".env", extra="ignore")

    admin_key: str = "dev-admin-key"
    db_path: str = "./data/proagent.db"
    llm_provider: str = "mock"  # mock | openai
    llm_base_url: str = "https://api.openai.com/v1"
    llm_api_key: str = ""
    llm_model: str = "gpt-4o-mini"
    host: str = "0.0.0.0"
    port: int = 8080
    skills_dirs: str = "./skills,./verticals/example/skills,./verticals/support_desk/skills"
    agents_config: str = "./config/agents.yaml"
    fixtures_dir: str = "./fixtures"
    max_tool_rounds: int = 8  # specialist tool-calling loop limit

    # Recommendation execution. noop acknowledges approval in-process (no network),
    # which is what the mock demo uses. webhook POSTs only to executor_webhook_url.
    # off records the decision and does not call an executor.
    executor_mode: Literal["off", "noop", "webhook"] = "noop"
    executor_webhook_url: str = ""
    executor_webhook_timeout_s: float = Field(default=5.0, gt=0)
    executor_webhook_token: str = ""
    executor_on_approve: bool = True

    # KB retrieval. Default bm25 so installs and CI need no embedding service.
    # hybrid: alpha * norm(vector) + (1 - alpha) * norm(bm25)
    # vector: cosine on stored embeddings
    # stub: offline hash-bag. openai: POST {base}/embeddings (OpenAI-compatible).
    kb_retrieval: Literal["bm25", "hybrid", "vector"] = "bm25"
    kb_embed_backend: Literal["stub", "openai"] = "stub"
    kb_embed_dims: int = Field(default=64, ge=1, le=4096)
    # Model id sent as `model` when the backend is openai. Ignored by stub.
    kb_embed_model: str = "text-embedding-3-small"
    # Empty values reuse PROAGENT_LLM_BASE_URL and PROAGENT_LLM_API_KEY.
    kb_embed_base_url: str = ""
    kb_embed_api_key: str = ""
    kb_hybrid_alpha: float = Field(default=0.5, ge=0.0, le=1.0)

    # In-process fixed window per tenant (admin and anonymous are separate buckets).
    # Not shared across processes and not a Redis quota. Restart clears the counters.
    rate_limit_enabled: bool = True
    rate_limit_requests: int = Field(default=120, ge=1)
    rate_limit_window_seconds: int = Field(default=60, ge=1)

    @field_validator("kb_retrieval", "kb_embed_backend", "executor_mode", mode="before")
    @classmethod
    def _normalize_choice(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip().lower()
        return value

    @property
    def skills_dir_list(self) -> List[Path]:
        return [Path(p.strip()) for p in self.skills_dirs.split(",") if p.strip()]

    @property
    def db_path_resolved(self) -> Path:
        p = Path(self.db_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        return p


@lru_cache
def get_settings() -> Settings:
    return Settings()
