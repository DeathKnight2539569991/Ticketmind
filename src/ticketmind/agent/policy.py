"""Conservative deterministic guards, not a claim of semantic safety completeness."""
import re
from ticketmind.agent.proposals import Escalation
from ticketmind.agent.state import customer_fact_text


def validate_questions(proposal):
    """Pydantic owns shape/length; Semantic Judge owns question meaning."""
    if len(proposal.questions) != len(set(proposal.questions)):
        raise ValueError("追问列表不能包含完全重复的问题")


def escalation(reason, *, risks=None):
    return Escalation(next_step="escalate", reason=reason, reply="当前信息或执行限制不足以可靠处理，请转交人工核查。",
                      risk_flags=risks or [])


def input_risks(text):
    patterns = {"security": r"泄露|泄漏|被盗|入侵|leak|breach",
                "payment": r"重复扣款|支付矛盾|扣款.*失败|失败.*扣款|退款|double.charge",
                "permissions": r"(?:修改|变更|提升|增加|开通).{0,6}权限|提权|越权",
                "data_loss": r"数据.{0,4}(?:丢失|消失|被删)|误删|data.loss"}
    return [flag for flag, pattern in patterns.items() if re.search(pattern, text, re.I)]


def validate_query(query, state):
    # Reject invented error codes / versions; other facts remain a model + review obligation.
    tokens = re.findall(r"\b(?:[A-Z][A-Z0-9]*_[A-Z0-9_]+|[vV]?\d+(?:\.\d+)+)\b", query)
    facts = customer_fact_text(state)
    if any(token not in facts for token in tokens):
        raise ValueError("重检索不得补造错误码或版本")
