from pathlib import Path
from typing import Literal
from pydantic import BaseModel, Field,ConfigDict
from ticketmind.tickets.enums import TicketChannel
class CorpusModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,  
        str_min_length=1,
    )
class CaseRequest(CorpusModel):
    subject:str = Field(
        description="工单标题",max_length=500
    )
    body:str = Field(
        description="工单正文",
    )
    channel:TicketChannel = Field(
        description="工单渠道",
    )
    requester_role:str=Field(max_length=64)
class CaseResolution(CorpusModel):
    summary:str = Field(
        description="工单问题和现象的概括，不推断原因",
    )
    root_cause:str
    actions:list[str] = Field(min_length=1)
    verification:str
class HistoricalCase(CorpusModel):
    source_id:str
    synthetic:Literal[True]
    request:CaseRequest
    status:Literal["resolved"]
    resolution:CaseResolution
def load_historical_cases(path:Path) -> list[HistoricalCase]:
    cases:list[HistoricalCase] = []
    seen_ids:set[str] = set()
    with path.open(encoding="utf-8-sig") as file:
        for line_number, line in enumerate(file, start=1):
            if not line.strip():
                continue
            try:
                case = HistoricalCase.model_validate_json(line)
                if case.source_id in seen_ids:
                    raise ValueError(f"Duplicate source_id: {case.source_id}")
                seen_ids.add(case.source_id)
                cases.append(case)
            except ValueError as exc:
                 raise ValueError(
                    f"{path.name} 第 {line_number} 行无效：{exc}"
                ) from exc
    if not cases:
        raise ValueError(f"{path.name} 中未找到有效的历史工单")
    return cases
def build_case_text(case:HistoricalCase) -> str:
    actions = "\n".join(
        f"- {action}"
        for action in case.resolution.actions
    )

    return "\n\n".join(
        [
            f"标题：{case.request.subject}",
            f"问题描述：{case.request.body}",
            f"历史解决摘要：{case.resolution.summary}",
            f"历史根因：{case.resolution.root_cause}",
            f"处理步骤：\n{actions}",
            f"验证结果：{case.resolution.verification}",
        ]
    )