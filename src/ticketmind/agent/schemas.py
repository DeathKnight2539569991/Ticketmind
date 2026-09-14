from pydantic import BaseModel, ConfigDict, Field


class TicketUnderstanding(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
    )

    summary: str = Field(
        min_length=1,
        description="概括工单明确描述的问题和现象，不推断原因",
    )
    error_codes: list[str] = Field(
        description="原文明确出现的错误码，保留原样；未提供则为空列表",
    )
    environment: list[str] = Field(
        description="原文明确提供的运行环境信息；未提供则为空列表",
    )