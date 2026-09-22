from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator
from ticketmind.core.text import CONVERSATION_MAX_CHARS, Text


MessageText = Text
SubjectText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]


class AgentMessage(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    role: Literal["customer", "support"]
    content: MessageText


class AgentRunInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    subject: SubjectText
    messages: list[AgentMessage] = Field(min_length=1)

    @model_validator(mode="after")
    def require_customer_message(self):
        if not any(message.role == "customer" for message in self.messages):
            raise ValueError("Agent 输入必须至少包含一条客户消息")
        if len(self.subject) + sum(len(message.content) for message in self.messages) > CONVERSATION_MAX_CHARS:
            raise ValueError(f"完整工单上下文不能超过 {CONVERSATION_MAX_CHARS} 字符，请转人工处理")
        return self
