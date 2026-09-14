"""Explicit real decision cache for acceptance scripts, never wired into production."""
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from ticketmind.agent.decide import DECISION_OPTIONS, decide_ticket, decision_messages
from ticketmind.agent.proposals import Proposal, validate_proposal
from ticketmind.agent.run_cache import calculate_request_fingerprint, save_cache


class DecisionCache(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_fingerprint: str
    proposal: Proposal
    usage: dict | None


class CachedDecision:
    def __init__(self, settings, path: Path, *, allow_call=False):
        self.settings, self.path, self.allow_call = settings, path, allow_call
        self.calls = self.cache_hits = 0

    def __call__(self, state, timeout, usage):
        system, user = decision_messages(state)
        fingerprint = calculate_request_fingerprint({
            "cache_version": 1, "model": self.settings.model,
            "endpoint": f"https://{self.settings.workspace_id}.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            **DECISION_OPTIONS, "response_format": {"type": "json_object"},
        })
        if self.path.exists():
            cache = DecisionCache.model_validate_json(self.path.read_text(encoding="utf-8"))
            if cache.request_fingerprint != fingerprint:
                raise ValueError("决策缓存输入、证据或模型配置不匹配")
            validate_proposal(cache.proposal, {hit.source_id for hit in state["retrieval_hits"]})
            self.cache_hits += 1
            usage["decision_cache_replay"] = True
            usage["cached_decision_usage"] = cache.usage
            return cache.proposal
        if not self.allow_call or self.calls >= 1:
            raise RuntimeError("缺少决策缓存；必须明确授权 --allow-decision，最多一次尝试")
        self.calls += 1
        proposal = decide_ticket(self.settings, state, timeout=timeout,
                                 usage_callback=lambda value: usage.update(decision=value))
        save_cache(self.path, DecisionCache(request_fingerprint=fingerprint, proposal=proposal, usage=usage.get("decision")))
        return proposal
