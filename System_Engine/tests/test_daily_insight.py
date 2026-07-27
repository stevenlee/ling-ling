"""Daily insight result semantics at the scheduler boundary."""

from agents.insight_agent import InsightGenerationFailure
from maintenance.daily_insight import run_daily_insight


class _LLM:
    trace_store = None


class _Sampler:
    def __init__(self, *_args):
        pass

    def select_targets(self, _limit):
        return ["Doc"]


class _FailingAgent:
    strategies = {"montecarlo": {}}

    def __init__(self, *_args):
        pass

    def generate_insight(self, *_args, **_kwargs):
        return InsightGenerationFailure("Error: Request timed out.")


def test_failed_generation_is_reported_as_failed(monkeypatch):
    monkeypatch.setattr("services.seed_sampler.SeedSampler", _Sampler)
    monkeypatch.setattr("agents.insight_agent.InsightAgent", _FailingAgent)

    result = run_daily_insight(_LLM(), object())

    assert result.status == "failed"
    assert result.summary == "Error: Request timed out."
