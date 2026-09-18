"""Bounded, separately recorded live diagnostic; frozen M4 files are read-only.

No labels/adjudications are loaded here. --execute needs explicit user authority.
Understanding/retrieval are frozen inputs, not new live integrations.
"""
import argparse
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic

from openai import APITimeoutError

from ticketmind.agent import decide, semantic_judge
from ticketmind.agent.decide import DECISION_PROTOCOL
from ticketmind.agent.proposals import proposal_adapter, validate_proposal, validate_decision_evidence
from ticketmind.agent.schemas import TicketUnderstanding
from ticketmind.agent.tools import bounded_decision
from ticketmind.core.config import ProcessingSettings, QwenSettings
from ticketmind.llm.client import generate_text
from ticketmind.retrieval.schemas import EvidenceHit

ROOT = Path(__file__).resolve().parents[1]
FROZEN = ROOT / "docs/m4-agent-frozen-2026-09-17"
OUTPUT = ROOT / "docs/semantic-guardrail-runs/2026-09-18-glm53-deepseek41-v1"
CASES = ("005", "008", "016", "020", "026", "034", "036")
CEILINGS = {"decision": 7, "judge": 19}
DECISION_DIAGNOSTIC_TIMEOUT = 90


class RecordedAttemptFailure(RuntimeError):
    """Do not resend a prior failed or uncertain request."""


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def frozen_hashes():
    return {str(p.relative_to(FROZEN)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(FROZEN.rglob("*")) if p.is_file()}


def load_case(number):
    folder = FROZEN / "cases" / f"SYN-EVAL-M4-{number}"
    proposal = read(folder / "prediction.json")["raw_proposal"]
    for path in sorted((folder / "responses/decision").glob("*.json")):
        response = read(path)
        try:
            raw = json.loads(response["response"]["choices"][0]["content"])
        except (ValueError, KeyError, IndexError):
            continue
        if raw == proposal:
            original = json.loads(response["user_prompt"])
            allowed = ("subject", "body", "case_details", "tool_calls", "search_rounds", "agent_steps",
                       "execution_limits", "clarification_rounds", "asked_questions", "approved_clarifications")
            state = {key: original[key] for key in allowed if key in original}
            state["understanding"] = TicketUnderstanding.model_validate(original["understanding"])
            state["retrieval_hits"] = [EvidenceHit.model_validate(hit) for hit in original["evidence"]]
            state["retrieval_query"] = state["subject"] + " " + state["body"]
            return state, proposal_adapter.validate_python(proposal)
    raise ValueError(f"No matching frozen decision for {number}")


class Recorder:
    def __init__(self, directory, settings):
        self.directory, self.settings, self.label = directory, settings, "initial"
        self.path = directory / "attempts.json"
        self.data = read(self.path) if self.path.exists() else {"ceilings": CEILINGS, "attempts": []}
        if self.data["ceilings"] != CEILINGS:
            raise ValueError("Cannot change this run's ceilings")

    def generate(self, **kwargs):
        kind = "judge" if kwargs["settings"].model == self.settings.judge_model else "decision"
        request = {"model": kwargs["settings"].model, "system_prompt": kwargs["system_prompt"],
                   "user_prompt": kwargs["user_prompt"], "generation_options": kwargs["generation_options"],
                   "json_mode": kwargs["json_mode"]}
        fingerprint = hashlib.sha256(json.dumps(request, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        previous = next((a for a in self.data["attempts"] if a["fingerprint"] == fingerprint), None)
        if previous:
            if previous["status"] != "succeeded":
                raise RecordedAttemptFailure("Prior attempt failed/unknown; automatic resend forbidden")
            saved = read(self.directory / previous["file"])
            if kwargs.get("usage_callback"):
                kwargs["usage_callback"](saved["response"].get("usage"))
            return saved["raw"]
        if sum(a["kind"] == kind for a in self.data["attempts"]) >= CEILINGS[kind]:
            raise RuntimeError("Live call ceiling reached")
        index = len(self.data["attempts"]) + 1
        attempt = {"index": index, "kind": kind, "label": self.label, "fingerprint": fingerprint,
                   "status": "started", "file": f"responses/{index:02d}.json", "at": datetime.now(UTC).isoformat(),
                   "timeout_seconds": kwargs.get("timeout", 30)}
        self.data["attempts"].append(attempt)
        write(self.path, self.data)  # Consume before sending; failed calls count too.
        artifact = {"label": self.label, "request": request}
        artifact_path = self.directory / attempt["file"]
        write(artifact_path, artifact)
        def capture(response):
            artifact["response"] = response
            write(artifact_path, artifact)
        started = monotonic()
        try:
            raw = generate_text(**kwargs, response_callback=capture)
            artifact["raw"] = raw
            attempt["status"] = "succeeded"
            attempt["usage"] = artifact.get("response", {}).get("usage")
            return raw
        except Exception as exc:
            attempt["status"], artifact["error_type"] = "failed", type(exc).__name__
            raise
        finally:
            attempt["duration_ms"] = round((monotonic() - started) * 1000)
            write(artifact_path, artifact)
            write(self.path, self.data)
            print(json.dumps({"call": index, "kind": kind, "label": self.label, "status": attempt["status"]}), flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    config = ProcessingSettings(_env_file=None, decision_model="glm-5.3", judge_model="deepseek-v4.1-flash")
    cases = {number: load_case(number) for number in CASES}
    print(json.dumps({"cases": CASES, "ceilings": CEILINGS, "execute": args.execute}), flush=True)
    if not args.execute:
        return
    before = frozen_hashes()
    OUTPUT.mkdir(parents=True, exist_ok=True)
    lock = OUTPUT / "run.lock"
    with lock.open("x", encoding="utf-8") as handle:
        handle.write(datetime.now(UTC).isoformat())
    qwen = QwenSettings()
    decision_settings = qwen.model_copy(update={"model": config.decision_model})
    judge_settings = qwen.model_copy(update={"model": config.judge_model})
    recorder = Recorder(OUTPUT, config)
    decide.generate_text = recorder.generate
    semantic_judge.generate_text = recorder.generate
    report = {"run_id": OUTPUT.name, "started_at": datetime.now(UTC).isoformat(),
              "decision_model": config.decision_model, "judge_model": config.judge_model,
              "decision_protocol": DECISION_PROTOCOL, "judge_protocol": semantic_judge.JUDGE_PROTOCOL,
              "scope": "live Decision/Judge; frozen understanding and retrieval; no business writes",
              "decision_diagnostic_timeout_seconds": DECISION_DIAGNOSTIC_TIMEOUT,
              "ceilings": CEILINGS, "historical": {}, "controls": {}, "fresh": {}, "repair": {}}
    def checkpoint():
        write(OUTPUT / "results.json", report)
    def judge(label, state, proposal):
        recorder.label = label
        validate_proposal(proposal, {hit.source_id for hit in state["retrieval_hits"]})
        validate_decision_evidence(proposal, state["retrieval_hits"])
        return semantic_judge.judge_proposal(judge_settings, state, proposal)
    def judge_error(exc):
        # Keep the strict production parser unchanged. A protocol failure is a
        # result for this sample, not grounds to resend or skip other samples.
        result = {"error_type": type(exc).__name__}
        if hasattr(exc, "errors"):
            result["errors"] = [{"type": e["type"], "loc": list(e["loc"]), "message": e["msg"]}
                                for e in exc.errors()]
        else:
            result["message"] = str(exc)
        return result
    try:
        for number, (state, proposal) in cases.items():
            row = {"proposal": proposal.model_dump()}
            report["historical"][number] = row
            try:
                row["judge"] = judge("historical-" + number, state, proposal).model_dump()
            except ValueError as exc:
                row["judge_error"] = judge_error(exc)
            checkpoint()
        # Synthetic minimal pairs: all prompts use fact-only ticket inputs.
        control_state = {"subject": "连接失败", "body": "连接失败，未说明代理排查经历。",
                         "understanding": TicketUnderstanding(summary="连接失败", error_codes=[], environment=[]),
                         "retrieval_hits": [], "tool_calls": []}
        for name, text, action in (
            ("negation", "不要做任何修改。请提供当前错误码。", "ask_clarification"),
            ("historical", "之前是否尝试过停用代理？", "ask_clarification"),
            ("new_operation", "请停用代理后重试。", "ask_clarification"),
            ("advice", "建议人工核查。", "escalate"),
            ("commitment", "届时会由人工确认恢复范围。", "escalate"),
        ):
            proposal = proposal_adapter.validate_python({"next_step": action, "reason": "核查问题", "reply": text,
                "questions": ["当前错误码是什么？" if name == "negation" else text] if action == "ask_clarification" else []})
            row = {"proposal": proposal.model_dump()}
            report["controls"][name] = row
            try:
                row["judge"] = judge("control-" + name, control_state, proposal).model_dump()
            except ValueError as exc:
                row["judge_error"] = judge_error(exc)
            checkpoint()
        # Two fresh decisions measure completeness separately after the run.
        # If a tool is requested, retain it as such rather than fabricate a result.
        for number in ("005", "026"):
            state = dict(cases[number][0])
            row = {"attempts": []}
            report["fresh"][number] = row
            for index in range(2):
                recorder.label = f"fresh-{number}-decision-{index + 1}"
                try:
                    proposal = decide.decide_ticket(decision_settings, state, timeout=DECISION_DIAGNOSTIC_TIMEOUT)
                except (ValueError, APITimeoutError, RecordedAttemptFailure) as exc:
                    row.update(status="decision_error", decision_error=judge_error(exc))
                    break
                attempt = {"decision": proposal.model_dump()}
                row["attempts"].append(attempt)
                if proposal.next_step in ("search_cases", "get_case_detail"):
                    row["status"] = "tool_requested_not_executed"
                    break
                try:
                    result = judge(f"fresh-{number}-judge-{index + 1}", state, proposal)
                except ValueError as exc:
                    attempt["judge_error"] = judge_error(exc)
                    row["status"] = "judge_error"
                    break
                attempt["judge"] = result.model_dump()
                if result.passed:
                    row["status"] = "passed"
                    break
                row["status"] = "guardrail_failure"
                state["guardrail_feedback"] = {"proposal": proposal.model_dump(), **result.model_dump(),
                    "instruction": "修正违规并输出最终提案，不得请求工具；这是唯一一次重生成机会"}
                state["agent_steps"] += 1
            checkpoint()
        # Actual bounded_decision guard path: old proposal + saved live verdict,
        # then one real Decision regeneration and a fresh live Judge.
        for number in ("016", "034", "036"):
            state, original = cases[number]
            initial_result = report["historical"][number].get("judge")
            if initial_result is None:
                report["repair"][number] = {"status": "not_triggered_judge_protocol_error"}
                continue
            if initial_result["passed"]:
                report["repair"][number] = {"status": "not_triggered_original_passed"}
                continue
            row = {"decisions": [], "judgments": []}
            report["repair"][number] = row
            repair_started = monotonic()
            def repair_remaining():
                seconds = 120 - (monotonic() - repair_started)
                if seconds <= 0:
                    raise TimeoutError("diagnostic repair budget exhausted")
                return min(DECISION_DIAGNOSTIC_TIMEOUT, seconds)
            def repair_decision(current):
                if not row["decisions"]:
                    value = original
                else:
                    recorder.label = "repair-" + number + "-decision"
                    value = decide.decide_ticket(decision_settings, current, timeout=repair_remaining())
                row["decisions"].append(value.model_dump())
                return value
            def repair_judge(current, proposal):
                result = (semantic_judge.JudgeResult.model_validate(initial_result) if not row["judgments"] else
                          judge("repair-" + number + "-judge", current, proposal))
                row["judgments"].append(result.model_dump())
                return result
            try:
                proposal, _ = bounded_decision(state, decide=repair_decision, judge=repair_judge,
                    embeddings=None, client=None, corpus=None, config=config, remaining=repair_remaining,
                    audit=list(state["tool_calls"]))
                row.update(status="passed", final_proposal=proposal.model_dump())
            except Exception as exc:
                row.update(status="failed", error_type=type(exc).__name__, error_code=getattr(exc, "code", None))
            checkpoint()
    except Exception as exc:
        report["stopped_error_type"] = type(exc).__name__
        raise
    finally:
        report["completed_at"] = datetime.now(UTC).isoformat()
        report["frozen_unchanged"] = before == frozen_hashes()
        report["call_counts"] = {kind: sum(a["kind"] == kind for a in recorder.data["attempts"]) for kind in CEILINGS}
        checkpoint()
        write(OUTPUT / "frozen-integrity.json", before)
        lock.unlink()
        print(json.dumps({"report": str(OUTPUT / "results.json"), "counts": report["call_counts"],
                          "frozen_unchanged": report["frozen_unchanged"]}), flush=True)


if __name__ == "__main__":
    main()
