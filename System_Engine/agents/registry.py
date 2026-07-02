from agents.merge_agent import MergeAgent
from agents.tag_patrol_agent import TagPatrolAgent
from agents.insight_agent import InsightAgent
from agents.linter_agent import LinterAgent
from agents.counter_agent import CounterAgent
from agents.planner_agent import PlannerAgent
from agents.executor_agent import ExecutorAgent
from agents.profiles_agent import ProfilesAgent
from agents.cortex_agent import CortexAgent
from agents.recall_agent import RecallAgent
from agents.tension_agent import TensionAgent
from agents.visualize_agent import VisualizeAgent
from agents.improve_agent import ImproveAgent
from agents.review_agent import ReviewAgent
from agents.blog_agent import BlogAgent


class AgentRegistry:
    def __init__(self, llm, rag):
        self.llm = llm
        self.rag = rag
        self._registry = {
            "merge": MergeAgent,
            "patrol_tags": TagPatrolAgent,
            "insight": InsightAgent,
            "patrol": LinterAgent,
            "linter": LinterAgent,
            "lens": CounterAgent,
            "plan": PlannerAgent,
            "do": ExecutorAgent,
            "profiles": ProfilesAgent,
            "cortex": CortexAgent,
            "recall": RecallAgent,
            "tensions": TensionAgent,
            "visualize": VisualizeAgent,
            "improve": ImproveAgent,
            "review": ReviewAgent,
            "blog": BlogAgent,
        }

    def get_agent(self, command_key: str):
        agent_class = self._registry.get(command_key)
        if agent_class:
            return agent_class(self.llm, self.rag)
        return None

    def list_commands(self):
        return list(self._registry.keys())
