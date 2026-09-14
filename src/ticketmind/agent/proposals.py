from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, TypeAdapter, model_validator

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=8000)]
SourceId = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)]


class ProposalBase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: Text
    reply: Text
    evidence_ids: list[SourceId] = Field(default_factory=list, max_length=100)
    risk_flags: list[Literal["security", "payment", "permissions", "data_loss"]] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique_evidence(self):
        if len(self.evidence_ids) != len(set(self.evidence_ids)):
            raise ValueError("引用来源不能重复")
        return self


class Resolution(ProposalBase):
    next_step: Literal["propose_resolution"]
    evidence_ids: list[SourceId] = Field(min_length=1, max_length=100)
    questions: list[Text] = Field(default_factory=list, max_length=0)


class Clarification(ProposalBase):
    next_step: Literal["ask_clarification"]
    questions: list[Text] = Field(min_length=1, max_length=5)


class Escalation(ProposalBase):
    next_step: Literal["escalate"]
    questions: list[Text] = Field(default_factory=list, max_length=0)


Proposal = Annotated[Resolution | Clarification | Escalation, Field(discriminator="next_step")]
proposal_adapter = TypeAdapter(Proposal)


def validate_proposal(proposal: Proposal, source_ids: set[str]) -> None:
    if not set(proposal.evidence_ids) <= source_ids:
        raise ValueError("提案引用了本次检索中不存在的来源")
    if proposal.risk_flags and proposal.next_step != "escalate":
        raise ValueError("存在高风险标记时必须提出转人工建议")
