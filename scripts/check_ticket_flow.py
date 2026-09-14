"""Real HTTP + isolated PostgreSQL + M0 model cache + live Milvus + optional one decision."""
import argparse
import json
import secrets
import socket
from pathlib import Path
from threading import Thread
from time import monotonic, sleep
from uuid import UUID, uuid4

import httpx
import uvicorn

from check_ticket_graph import sample_state
from ticketmind.agent.dev_cache import CachedQueryEmbeddings, CachedUnderstanding
from ticketmind.agent.dev_decision_cache import CachedDecision
from ticketmind.agent.retrieve import build_retrieval_query
from ticketmind.agent.runtime import AgentRunner
from ticketmind.core.config import AuthSettings, MilvusSettings, ProcessingSettings, QwenSettings
from ticketmind.db.testing import isolated_database
from ticketmind.main import create_app
from ticketmind.tickets.models import ProcessingResult


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-decision", action="store_true", help="已获授权时最多新增一次决策调用")
    parser.add_argument("--check-only", action="store_true", help="仅校验配置和匹配的 M0 缓存，不访问网络/数据库")
    parser.add_argument("--decision-cache", type=Path, default=Path("data/cache/graph/api_timeout/decision_m2.json"),
                        help="独立 M2 决策缓存；旧 M1 缓存不覆盖、不修改指纹")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    qwen, milvus, config = QwenSettings(), MilvusSettings(), ProcessingSettings()
    initial = sample_state()
    understanding = CachedUnderstanding(root / "data/cache/graph/api_timeout/understanding.json")
    embedding = CachedQueryEmbeddings(qwen, root / "data/cache/graph/api_timeout/query.json",
                                      factory=lambda: (_ for _ in ()).throw(RuntimeError("禁止新 Embedding 调用")))
    if understanding.read(settings=qwen, **initial) is None or embedding.read(build_retrieval_query(**initial)) is None:
        raise RuntimeError("缺少匹配的 M0 真实缓存，禁止自动补调模型")
    decision = CachedDecision(qwen, args.decision_cache, allow_call=args.allow_decision)
    runner = AgentRunner(qwen, milvus, config, understanding_fn=understanding,
                         embedding_factory=lambda remaining: embedding, decision_fn=decision)
    if args.check_only:
        print("配置、语料、两份 M0 真实缓存校验通过；未连接数据库/Milvus/模型；决策缓存需实际检索后校验。")
        return
    if not args.allow_decision and not decision.path.exists():
        raise RuntimeError("缺少决策缓存；必须先授权一次决策调用，再使用 --allow-decision")
    token = secrets.token_urlsafe(32)
    auth = AuthSettings(_env_file=None, operator_token=token)
    report = None
    try:
        with isolated_database() as (engine, factory, schema):
            application = create_app(session_factory=factory, runner=runner, auth_settings=auth, processing_settings=config)
            server = uvicorn.Server(uvicorn.Config(application, host="127.0.0.1", port=0, log_level="warning", access_log=False))
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
                thread = Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
                thread.start()
                try:
                    deadline = monotonic() + 10
                    while not server.started and thread.is_alive() and monotonic() < deadline:
                        sleep(0.05)
                    if not server.started:
                        raise RuntimeError("本地 HTTP 验收服务未启动")
                    with httpx.Client(base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=110,
                                      headers={"Authorization": f"Bearer {token}"}) as client:
                        created = client.post("/tickets", headers={"Idempotency-Key": uuid4().hex},
                                              json={**initial, "channel": "api", "requester_role": "synthetic-user"})
                        assert created.status_code == 201, created.text
                        ticket_id = created.json()["id"]
                        detail = client.get(f"/tickets/{ticket_id}").json()
                        assert detail["subject"] == initial["subject"] and detail["messages"][0]["body"] == initial["body"]
                        key = uuid4().hex
                        payload = {"trigger_message_id": detail["messages"][0]["id"], "expected_version": detail["version"]}
                        response = client.post(f"/tickets/{ticket_id}/runs", json=payload, headers={"Idempotency-Key": key})
                        assert response.status_code == 201, response.text
                        result = response.json()
                        assert result["run_status"] == "waiting_review", result
                        repeated = client.post(f"/tickets/{ticket_id}/runs", json=payload, headers={"Idempotency-Key": key})
                        assert repeated.status_code == 200 and repeated.json()["id"] == result["id"]
                        conflict = client.post(f"/tickets/{ticket_id}/runs", json={**payload, "expected_version": 2},
                                               headers={"Idempotency-Key": key})
                        assert conflict.status_code == 409
                        assert client.get(f'/tickets/{ticket_id}/runs/{result["id"]}').json() == result
                        final_ticket = client.get(f"/tickets/{ticket_id}").json()
                        assert final_ticket["status"] == "open" and len(final_ticket["messages"]) == 1
                        for hit in result["retrieval_evidence"]:
                            source = client.get(f'/sources/{hit["source_id"]}', params={"corpus_version": result["corpus_version"]})
                            assert source.status_code == 200
                        with factory() as session:
                            saved = session.get(ProcessingResult, UUID(result["id"]))
                            assert saved.proposal == result["proposal"] and saved.retrieval_evidence == result["retrieval_evidence"]
                            assert saved.input_snapshot["body"] == initial["body"]
                        report = {"verification": "real_http_postgresql_and_milvus_with_m0_model_cache",
                                  "isolated_schema": schema, "ticket": initial, "result": result,
                                  "idempotent_replay_status": repeated.status_code, "conflict_status": conflict.status_code,
                                  "ticket_status": final_ticket["status"]}
                        print(json.dumps(report, ensure_ascii=False, indent=2))
                finally:
                    server.should_exit = True
                    thread.join(timeout=15)
                    if thread.is_alive():
                        raise RuntimeError("验收 HTTP 服务未按期停止")
    finally:
        counts = {"understanding_calls": understanding.calls, "embedding_calls": embedding.calls,
                  "understanding_cache_hits": understanding.cache_hits, "embedding_cache_hits": embedding.cache_hits,
                  "decision_calls": decision.calls, "decision_cache_hits": decision.cache_hits}
        print(json.dumps(counts))
        if report is not None:
            report.update(counts)
            path = root / "data/cache/graph/api_timeout/m2_flow_verification.json"
            path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
