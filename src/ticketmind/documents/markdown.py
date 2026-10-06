"""Small, deterministic Markdown section/chunk parser for the demo corpus."""
import hashlib
import re
from dataclasses import dataclass

from ticketmind.retrieval.case_collection import TEXT_MAX_BYTES

CHUNK_MAX_BYTES = min(6000, TEXT_MAX_BYTES - 1024)
_HEADING = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$")


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ParsedChunk:
    chunk_id: str
    section: str
    text: str
    content_hash: str


@dataclass(frozen=True)
class ParsedDocument:
    doc_id: str
    title: str
    content_hash: str
    chunks: tuple[ParsedChunk, ...]


def _parts(text: str):
    """Split at paragraph boundaries, then at characters for one oversized paragraph."""
    for paragraph in re.split(r"\n\s*\n", text.strip()):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        current = ""
        for character in paragraph:
            if len((current + character).encode("utf-8")) > CHUNK_MAX_BYTES:
                yield current
                current = ""
            current += character
        if current:
            yield current


def parse_markdown(doc_id: str, markdown: str) -> ParsedDocument:
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", doc_id):
        raise ValueError("invalid_doc_id")
    normalized = markdown.replace("\r\n", "\n").replace("\r", "\n").strip() + "\n"
    lines = normalized.splitlines()
    if not lines or not (match := _HEADING.match(lines[0])) or len(match.group(1)) != 1:
        raise ValueError("document_requires_h1_title")
    title = match.group(2).strip()
    if not title:
        raise ValueError("document_requires_h1_title")
    sections: list[tuple[str, str]] = []
    path: dict[int, str] = {}
    body: list[str] = []
    in_fence = False
    for line in lines[1:]:
        heading = None if in_fence else _HEADING.match(line)
        if heading:
            if any(part.strip() for part in body):
                sections.append((" / ".join(path[level] for level in sorted(path)), "\n".join(body)))
            body = []
            level = len(heading.group(1))
            if level == 1:
                raise ValueError("document_has_multiple_h1_titles")
            path = {key: value for key, value in path.items() if key < level}
            path[level] = heading.group(2).strip()
        else:
            body.append(line)
        if line.lstrip().startswith(("```", "~~~")):
            in_fence = not in_fence
    if any(part.strip() for part in body):
        sections.append((" / ".join(path[level] for level in sorted(path)), "\n".join(body)))
    chunks: list[ParsedChunk] = []
    for section_number, (section, section_text) in enumerate(sections, 1):
        accumulated = ""
        chunk_number = 0
        def append_chunk(value: str):
            nonlocal chunk_number
            chunk_number += 1
            chunk_id = f"{doc_id}:{section_number:03d}:{chunk_number:03d}"
            chunks.append(ParsedChunk(chunk_id, section, value, digest(f"{title}\n{section}\n{value}")))
        for part in _parts(section_text):
            candidate = f"{accumulated}\n\n{part}" if accumulated else part
            if len(candidate.encode("utf-8")) > CHUNK_MAX_BYTES:
                append_chunk(accumulated)
                accumulated = part
            else:
                accumulated = candidate
        if accumulated:
            append_chunk(accumulated)
    if not chunks:
        raise ValueError("document_has_no_content")
    return ParsedDocument(doc_id, title, digest(normalized), tuple(chunks))
