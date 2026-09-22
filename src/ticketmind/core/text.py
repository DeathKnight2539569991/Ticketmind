"""Shared business text limits; never silently truncate customer history."""
from typing import Annotated

from pydantic import StringConstraints

MESSAGE_MAX_CHARS = 8000
CONVERSATION_MAX_CHARS = 32000
KNOWLEDGE_MAX_BYTES = 16384
Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MESSAGE_MAX_CHARS)]
