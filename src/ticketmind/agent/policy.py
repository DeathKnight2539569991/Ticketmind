"""Conservative deterministic guards, not a claim of semantic safety completeness."""
import re
from ticketmind.agent.proposals import Escalation, UnsupportedActionClaim


# Match assertion forms, not the presence of "人工/转交/联系" alone. These
# bounded, clause-local rules cover Chinese reply drafts; they are not an NLP
# truth checker. No external write/dispatch tools exist at proposal time.
_CLAUSE = r"[^，。！？；,.!?;：:]"
_ACTION = (
    r"(?:转交|转派|转人工|升级|提交|通知|联系|处理|执行|退款|退还|"
    r"恢复|删除|发送|派单|关闭工单|创建工单|"
    rf"(?:修改|更改|调整|开通|变更){_CLAUSE}{{0,12}}权限)"
)
_EXTERNAL_ACTOR = r"(?:我们|我方|客服|工作人员|技术人员|支持人员|工程师|专员|人工|团队)"
_FOLLOWUP = r"(?:联系|处理|回复|答复|跟进|核对|核查|通知|发送|退款|转交|提交|派单)"
_FUTURE = r"(?:将会|将|(?<!不)会|一定|保证|承诺)"
_REPLY_CLAIM_PATTERNS = tuple(re.compile(pattern) for pattern in (
    # 已将该工单转交 / 已为您退款 / 已经完成处理
    rf"(?:已经|已)(?:成功|完成|正式|自动)?"
    rf"(?:(?:将|把|为|替|帮|向|给|由|被|对){_CLAUSE}{{0,32}}?)?{_ACTION}",
    # Passive permission changes: 权限已经修改
    rf"权限{_CLAUSE}{{0,8}}(?:已经|已)(?:成功|完成)?(?:修改|更改|调整|开通|变更)",
    # 技术人员稍后会联系 / 支付支持人员会核对...后与您联系
    rf"{_EXTERNAL_ACTOR}{_CLAUSE}{{0,20}}?{_FUTURE}{_CLAUSE}{{0,32}}?{_FOLLOWUP}",
    # 稍后一定会有客服回复 / 将由工程师处理
    rf"{_FUTURE}{_CLAUSE}{{0,16}}?{_EXTERNAL_ACTOR}{_CLAUSE}{{0,24}}?{_FOLLOWUP}",
))


def validate_reply_claims(reply: str) -> None:
    """Reject unsupported assertions without rewriting or trusting model flags.

    Deliberately only validates new proposals, not historical row deserialization
    or reviewer edits. Quoted/reported facts and unusual syntax still need review.
    """
    text = re.sub(r"\s+", "", reply)
    if any(pattern.search(text) for pattern in _REPLY_CLAIM_PATTERNS):
        raise UnsupportedActionClaim()


def validate_questions(proposal):
    text = "\n".join([proposal.reply, *proposal.questions])
    if re.search(r"停用|禁用|关闭|卸载|删除|重装|重启|重置|修改|更改|调整|调高|调低|调大|调小|切换|绕过|执行|运行命令|尝试|disable|turn\s+off|uninstall|delete|restart|reset|adjust|increase|decrease|bypass|run\s+.*command", text, re.I):
        raise ValueError("追问只能收集现有事实，不得夹带操作建议")


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
    facts = state["subject"] + "\n" + state["body"]
    if any(token not in facts for token in tokens):
        raise ValueError("重检索不得补造错误码或版本")
