"""Explicit, idempotent Markdown import and derived-index reconciliation."""
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from ticketmind.documents.index import docs_collection, docs_manifest
from ticketmind.documents.markdown import parse_markdown
from ticketmind.documents.models import DEFAULT_DOCS_VERSION, DocsDataset, Document, DocumentChunk
from ticketmind.retrieval.schemas import RetrievalError


def import_markdown(factory, path: Path, *, version: str = DEFAULT_DOCS_VERSION) -> dict:
    """Import a directory as an additive corpus; changed files replace their own chunks."""
    path = Path(path)
    files = sorted(path.glob("*.md")) if path.is_dir() else [path]
    if not files or any(not file.is_file() or file.suffix.lower() != ".md" for file in files):
        raise ValueError("docs_markdown_files_missing")
    parsed = [(file, parse_markdown(file.stem, file.read_text(encoding="utf-8"))) for file in files]
    if len({document.doc_id for _, document in parsed}) != len(parsed):
        raise ValueError("duplicate_doc_id")
    changed = 0
    with factory() as session, session.begin():
        session.execute(insert(DocsDataset).values(version=version, manifest=docs_manifest(version),
            collection_name=docs_collection(version), synthetic=True).on_conflict_do_nothing())
        dataset = session.get(DocsDataset, version, with_for_update=True)
        if dataset.manifest != docs_manifest(version) or dataset.collection_name != docs_collection(version) or not dataset.synthetic:
            raise RetrievalError("docs_dataset_version_mismatch")
        for file, item in parsed:
            source_path = file.name
            document = session.get(Document, (version, item.doc_id))
            if document is None:
                document = Document(dataset_version=version, doc_id=item.doc_id, source_path=source_path,
                                    title=item.title, content_hash=item.content_hash, revision=1)
                session.add(document)
                session.flush()
                changed += 1
            elif document.content_hash != item.content_hash or document.title != item.title:
                document.source_path = source_path
                document.title = item.title
                document.content_hash = item.content_hash
                document.revision += 1
                changed += 1
            existing = {chunk.chunk_id: chunk for chunk in session.scalars(select(DocumentChunk).where(
                DocumentChunk.dataset_version == version, DocumentChunk.doc_id == item.doc_id)).all()}
            incoming = {chunk.chunk_id for chunk in item.chunks}
            for stale_id in existing.keys() - incoming:
                session.delete(existing[stale_id])
            for item_chunk in item.chunks:
                chunk = existing.get(item_chunk.chunk_id)
                if chunk is None:
                    session.add(DocumentChunk(dataset_version=version, doc_id=item.doc_id,
                        chunk_id=item_chunk.chunk_id, section=item_chunk.section, text=item_chunk.text,
                        content_hash=item_chunk.content_hash, revision=1))
                elif (chunk.content_hash != item_chunk.content_hash or chunk.section != item_chunk.section
                      or chunk.text != item_chunk.text):
                    chunk.section = item_chunk.section
                    chunk.text = item_chunk.text
                    chunk.content_hash = item_chunk.content_hash
                    chunk.indexed_hash = None
                    chunk.dense_indexed_hash = None
                    chunk.revision += 1
    return {"docs_version": version, "documents": len(parsed), "changed": changed,
            "chunks": sum(len(document.chunks) for _, document in parsed)}


def sync_docs(factory, index, *, version: str = DEFAULT_DOCS_VERSION) -> dict:
    """Rebuild missing BM25 rows without embedding/model calls."""
    with factory() as session:
        dataset = session.get(DocsDataset, version)
        if dataset is None:
            raise RetrievalError("docs_dataset_missing")
    index.ensure(dataset)
    indexed = 0
    with factory() as session:
        rows = session.execute(select(DocumentChunk, Document).join(Document,
            (DocumentChunk.dataset_version == Document.dataset_version) & (DocumentChunk.doc_id == Document.doc_id))
            .where(DocumentChunk.dataset_version == version).order_by(DocumentChunk.chunk_id)).all()
    for chunk, document in rows:
        if chunk.indexed_hash == chunk.content_hash and index.matches(dataset, chunk):
            continue
        index.upsert(dataset, document, chunk)
        with factory() as session, session.begin():
            current = session.get(DocumentChunk, (version, chunk.doc_id, chunk.chunk_id), with_for_update=True)
            if current is not None and current.content_hash == chunk.content_hash:
                current.indexed_hash = chunk.content_hash
                indexed += 1
    return {"docs_version": version, "indexed": indexed, "total": len(rows)}
