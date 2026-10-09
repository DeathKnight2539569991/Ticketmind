import json
import shutil
from pathlib import Path

import pytest
from pydantic import ValidationError

from scripts import phase7_offline as p7

ORIGINAL_ROOT = p7.ROOT
ORIGINAL_DATA = p7.DATA
ORIGINAL_OLD = p7.OLD


def _sandbox(tmp_path, monkeypatch):
    root = tmp_path / "app"
    shutil.copytree(ORIGINAL_ROOT / "docs", root / "docs")
    shutil.copytree(ORIGINAL_ROOT / "scripts", root / "scripts")
    shutil.copytree(ORIGINAL_DATA, root / "data/synthetic/phase7_v1")
    # Each test starts with candidate data; real corpus approvals belong to its
    # frozen snapshot and must not authorize the temporary test mutations.
    (root / "data/synthetic/phase7_v1/label_reviews.jsonl").unlink(missing_ok=True)
    shutil.copytree(ORIGINAL_OLD, root / "data/synthetic/v2")
    for legacy in ("m4", "live_e2e_v1", "live_e2e_v2", "m3"):
        shutil.copytree(ORIGINAL_ROOT / "data/synthetic" / legacy, root / "data/synthetic" / legacy)
    monkeypatch.setattr(p7, "ROOT", root)
    monkeypatch.setattr(p7, "DATA", root / "data/synthetic/phase7_v1")
    monkeypatch.setattr(p7, "OLD", root / "data/synthetic/v2")
    monkeypatch.setattr(p7, "RULES", root / "docs/phase7-data-contract.md")
    return root / "data/synthetic/phase7_v1"


def _edit_jsonl(path, index, update):
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    update(rows[index])
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")


def test_candidate_data_builds_stable_test_only_artifacts():
    manifest, queries, report, _, _ = p7.validate_data()
    assert manifest["counts"]["documents"] == 8
    assert manifest["counts"]["document_chunks"] == 24
    assert manifest["counts"]["historical_cases"] == 120
    assert manifest["counts"]["tickets"] == 48
    assert len(queries) == 48 and all(row["test_only"] and row["split"] == "test" for row in queries)
    assert queries[0]["query"].startswith("标题：")
    assert "expected_action" not in queries[0]["query"]
    assert report["review_status"] == "candidate_unreviewed"
    assert p7.snapshot_hash(manifest) == p7.snapshot_hash(json.loads(p7.canonical(manifest)))


@pytest.mark.parametrize("mutation, message", [
    (lambda row: row["input"].update({"expected_action": "escalate"}), "extra_forbidden|label leakage"),
    (lambda row: row.update({"split": "validation"}), "test-only"),
])
def test_bad_ticket_source_rejected(tmp_path, monkeypatch, mutation, message):
    data = _sandbox(tmp_path, monkeypatch)
    _edit_jsonl(data / "evaluation_cases.jsonl", 0, mutation)
    with pytest.raises((ValueError, ValidationError), match=message):
        p7.validate_data()


def test_case_label_reference_overlap_rejected(tmp_path, monkeypatch):
    data = _sandbox(tmp_path, monkeypatch)
    _edit_jsonl(data / "evaluation_labels.jsonl", 0,
                lambda row: row["distractor_source_ids"].append(row["relevant_source_ids"][0]))
    with pytest.raises(ValueError, match="exclusive"):
        p7.validate_data()


def test_unknown_doc_reference_and_excess_tool_path_rejected(tmp_path, monkeypatch):
    data = _sandbox(tmp_path / "unknown-doc", monkeypatch)
    _edit_jsonl(data / "evaluation_labels.jsonl", 0,
                lambda row: row["relevant_doc_chunk_ids"].__setitem__(0, "missing_doc:001:001"))
    with pytest.raises(ValueError, match="unknown|exist"):
        p7.validate_data()
    data = _sandbox(tmp_path / "bad-path", monkeypatch)
    _edit_jsonl(data / "evaluation_labels.jsonl", 0,
                lambda row: row["allowed_followup_tool_paths"].append(["search_cases", "search_cases"]))
    with pytest.raises(ValueError, match="allowed_followup_tool_paths"):
        p7.validate_data()


def test_invalid_historical_schema_rejected(tmp_path, monkeypatch):
    data = _sandbox(tmp_path, monkeypatch)
    _edit_jsonl(data / "historical_cases.jsonl", 0, lambda row: row.update({"expected_action": "escalate"}))
    with pytest.raises(ValueError, match="extra_forbidden"):
        p7.validate_data()


def test_changed_docs_and_rules_make_frozen_snapshot_stale(tmp_path, monkeypatch):
    data = _sandbox(tmp_path, monkeypatch)
    p7.freeze()
    doc = data / "docs/import_schema.md"
    doc.write_text(doc.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="manifest is stale"):
        p7.check_frozen()
    p7.freeze()
    rules = p7.RULES
    rules.write_text(rules.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="manifest is stale"):
        p7.check_frozen()


def test_require_reviewed_rejects_absent_or_stale_approval(tmp_path, monkeypatch):
    data = _sandbox(tmp_path, monkeypatch)
    p7.freeze()
    with pytest.raises(ValueError, match="approval records are required"):
        p7.check_frozen(require_reviewed=True)
    manifest, _, _, labels, aux = p7.validate_data()
    cases = {row["case_id"]: row for row in aux["cases"]}
    records = []
    for label in labels:
        records.append({"case_id": label["case_id"], "reviewer": "review-agent-1",
                        "review_method": "independent_agent", "reviewed_at": "2026-10-06T10:00:00Z",
                        "decision": "approve", "label_hash": p7.label_hash(
                            cases[label["case_id"]], label, corpus_version=aux["corpus"].version),
                        "snapshot_hash": p7.snapshot_hash(manifest), "rationale": "checked"})
    (data / "label_reviews.jsonl").write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    assert p7.check_frozen(require_reviewed=True)["review_status"] == "reviewed"
    records[0]["snapshot_hash"] = "0" * 64
    (data / "label_reviews.jsonl").write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    with pytest.raises(ValueError, match="stale"):
        p7.check_frozen()


def test_reviewed_export_requires_approval_and_protects_snapshot_files(tmp_path, monkeypatch):
    data = _sandbox(tmp_path, monkeypatch)
    p7.freeze()
    with pytest.raises(ValueError, match="collides"):
        p7.export_reviewed_queries(data / "manifest.json")
    with pytest.raises(ValueError, match="approval records are required"):
        p7.export_reviewed_queries(tmp_path / "queries.jsonl")


def test_freeze_refuses_existing_unbound_review_record(tmp_path, monkeypatch):
    data = _sandbox(tmp_path, monkeypatch)
    p7.freeze()
    (data / "label_reviews.jsonl").write_text('{"case_id":"old"}\n', encoding="utf-8")
    before = (data / "manifest.json").read_bytes()
    with pytest.raises(ValueError, match="approval records exist|review records"):
        p7.freeze()
    assert (data / "manifest.json").read_bytes() == before


def test_freeze_and_check_do_not_use_network_or_database(tmp_path, monkeypatch):
    data = _sandbox(tmp_path, monkeypatch)
    import socket

    def blocked(*args, **kwargs):
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket, "socket", blocked)
    p7.freeze()
    assert p7.check_frozen()["status"] == "ok"
    assert not (data / "label_reviews.jsonl").exists()
