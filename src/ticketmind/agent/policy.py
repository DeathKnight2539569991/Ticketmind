"""Deterministic query validation and bounded-loop escalation helpers."""
import re
from ticketmind.agent.proposals import Escalation
from ticketmind.agent.state import customer_fact_text


def escalation(reason):
    return Escalation(next_step="escalate", reason=reason, reply="当前信息或执行限制不足以可靠处理，请转交人工核查。")


def validate_query(query, state):
    # Reject invented error codes / versions; other facts remain a model + review obligation.
    tokens = re.findall(r"\b(?:[A-Z][A-Z0-9]*_[A-Z0-9_]+|[vV]?\d+(?:\.\d+)+)\b", query)
    facts = customer_fact_text(state)
    if any(token not in facts for token in tokens):
        raise ValueError("重检索不得补造错误码或版本")
