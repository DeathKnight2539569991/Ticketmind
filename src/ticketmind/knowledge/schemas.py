"""Human-authored searchable knowledge, separate from the original transcript."""
from pydantic import BaseModel, ConfigDict, Field

from ticketmind.core.text import KNOWLEDGE_MAX_BYTES, Text


class KnowledgeArticle(BaseModel):
    model_config = ConfigDict(extra="forbid")
    problem: Text
    applicability: Text
    solution: Text
    verification: Text

    def render(self, title):
        return (f"标题：{title}\n\n问题：{self.problem}\n\n适用条件：{self.applicability}"
                f"\n\n最终处理步骤：{self.solution}\n\n验证结果：{self.verification}")

    def validate_size(self, title):
        content = self.render(title)
        if len(content.encode("utf-8")) > KNOWLEDGE_MAX_BYTES:
            raise ValueError(f"知识正文不能超过 {KNOWLEDGE_MAX_BYTES} UTF-8 字节，请精简后批准")
        return content


class KnowledgeWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1)


class KnowledgeApprove(KnowledgeWrite):
    # Optional only to replay already-saved historical approval requests.
    # A new publication always requires a reviewed article.
    article: KnowledgeArticle | None = None
