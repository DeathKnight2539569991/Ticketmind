from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, TypeAdapter, model_validator

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=8000)]
SourceId = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)]
RiskFlag = Literal["security", "payment", "permissions", "data_loss"]
EvidenceQuote = Annotated[str, StringConstraints(strip_whitespace=True, min_length=12, max_length=2000)]


class ProposalBase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: Text
    reply: Text
    evidence_ids: list[SourceId] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def unique_evidence(self):
        if len(self.evidence_ids) != len(set(self.evidence_ids)):
            raise ValueError("引用来源不能重复")
        return self


class Resolution(ProposalBase):
    next_step: Literal["propose_resolution"]
    evidence_ids: list[SourceId] = Field(min_length=1, max_length=100)
    # Optional for reading historical M1/M2 rows; new model responses must provide
    # one verbatim support excerpt per citation (validated with actual hit text).
    evidence_quotes: dict[SourceId, EvidenceQuote] = Field(default_factory=dict)


class ModelResolution(Resolution):
    """Current model-output contract; historical stored rows may omit quotes."""
    evidence_quotes: dict[SourceId, EvidenceQuote]


class Clarification(ProposalBase):
    next_step: Literal["ask_clarification"]


class Escalation(ProposalBase):
    next_step: Literal["escalate"]
    risk_flags: list[RiskFlag] = Field(default_factory=list)


class ModelEscalation(ProposalBase):
    """Model-facing escalation; risk flags are deterministic runtime metadata."""
    next_step: Literal["escalate"]


Proposal = Annotated[Resolution | Clarification | Escalation, Field(discriminator="next_step")]
proposal_adapter = TypeAdapter(Proposal)

ModelProposal = Annotated[ModelResolution | Clarification | ModelEscalation, Field(discriminator="next_step")]
model_proposal_adapter = TypeAdapter(ModelProposal)


class SearchCases(BaseModel):
    model_config = ConfigDict(extra="forbid")
    next_step: Literal["search_cases"]
    reason: Text
    query: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1000)]


class GetCaseDetail(BaseModel):
    model_config = ConfigDict(extra="forbid")
    next_step: Literal["get_case_detail"]
    reason: Text
    source_id: SourceId


Decision = Annotated[
    ModelResolution | Clarification | ModelEscalation | SearchCases | GetCaseDetail,
    Field(discriminator="next_step"),
]
decision_adapter = TypeAdapter(Decision)


def validate_proposal(proposal: Proposal, source_ids: set[str]) -> None:
    if not set(proposal.evidence_ids) <= source_ids:
        raise ValueError("提案引用了本次检索中不存在的来源")


def validate_decision_evidence(decision, hits):
    """Verbatim provenance check, not a semantic entailment/safety guarantee."""
    if decision.next_step != "propose_resolution":
        return
    if set(decision.evidence_quotes) != set(decision.evidence_ids):
        raise ValueError("新的解决提案必须为每个引用提供实际来源原文")
    sources = {
        (hit["source_id"] if isinstance(hit, dict) else hit.source_id):
        (hit["text"] if isinstance(hit, dict) else hit.text)
        for hit in hits
    }
    if any(
        source_id not in sources or quote not in sources[source_id]
        for source_id, quote in decision.evidence_quotes.items()
    ):
        raise ValueError("解决提案引用原文不在对应的实际证据中")
