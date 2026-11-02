from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from proagent_os.config import get_settings
from proagent_os.harness.hitl import TaskHitlService
from proagent_os.harness.orchestrator import Orchestrator
from proagent_os.harness.registry import AgentRegistry
from proagent_os.llm.factory import get_llm
from proagent_os.professional.kb.store import KnowledgeBase
from proagent_os.professional.skills.loader import SkillLoader
from proagent_os.professional.tools.builtins import build_builtin_registry
from proagent_os.proactive.monitors import MonitorService
from proagent_os.proactive.recommendations import RecommendationService
from proagent_os.profitable.learning import LearningLoop
from proagent_os.profitable.metrics import MetricsAdapter


class AppServices:
    def __init__(self) -> None:
        settings = get_settings()
        self.settings = settings
        self.agents = AgentRegistry(Path(settings.agents_config))
        # Same discovery for every pack under verticals/*/agents/agents.yaml
        # (example, ops_incident, support_desk, and later packs). Cwd is the repo root.
        self.agents.merge_tree(Path("verticals"))
        self.tools = build_builtin_registry()
        self.skills = SkillLoader(settings.skills_dir_list)
        self.llm = get_llm()
        self.orchestrator = Orchestrator(self.llm, self.agents, self.tools, self.skills)
        self.kb = KnowledgeBase()
        self.monitors = MonitorService()
        self.recommendations = RecommendationService()
        self.metrics = MetricsAdapter()
        self.learning = LearningLoop()
        self.hitl = TaskHitlService()


@lru_cache
def get_services() -> AppServices:
    return AppServices()


def reset_services() -> AppServices:
    get_services.cache_clear()
    return get_services()
