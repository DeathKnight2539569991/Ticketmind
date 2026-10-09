"""E2E harness guards plus real isolated DB/Milvus with deterministic models."""
import importlib.util
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location('phase7_e2e_entry', SCRIPTS / 'evaluate_phase7_live.py')
entry = importlib.util.module_from_spec(spec)
spec.loader.exec_module(entry)

def test_unknown_usage_reservation_and_settled_usage_survive_reload(tmp_path):
    ledger = entry.CostLedger(tmp_path/'attempts.json', entry.CEILINGS)
    def unknown(record):
        ledger.reserve('decision', 1000, 1600)
        ledger.record_usage(None, 'decision')
    ledger.attempt('decision', 'unknown', unknown)
    reserved = ledger.data['attempts'][0]['reserved_cny']
    assert reserved > 0
    with pytest.raises(RuntimeError, match='禁止自动重试'):
        ledger.attempt('decision','unknown',unknown)
    def known(record):
        ledger.reserve('decision', 1000, 1600)
        ledger.record_usage({'prompt_tokens':100, 'completion_tokens':20}, 'decision')
    ledger.attempt('decision','known',known)
    reloaded = entry.CostLedger(tmp_path/'attempts.json',entry.CEILINGS)
    assert reloaded.data['attempts'][0]['reserved_cny']==reserved
    assert reloaded.data['attempts'][1]['reserved_cny']==0
    assert reloaded.data['settled_cost_cny']==pytest.approx(.000134)

def test_cost_cap_blocks_network_before_send(tmp_path):
    ledger=entry.CostLedger(tmp_path/'attempts.json',entry.CEILINGS)
    ledger.data['settled_cost_cny']=4.99
    sent=[]
    def operation(record):
        ledger.reserve('judge', 10000, 2000)
        sent.append(True)
    with pytest.raises(RuntimeError, match='cost_cap'):
        ledger.attempt('judge','blocked',operation)
    assert not sent
    assert ledger.data['attempts'][0]['status']=='failed'

def test_precall_resume_rejects_unknown_attempt_trace_and_response(tmp_path):
    row={'case_id':'sample', 'input':{'subject':'unique subject','body':'unique body'}}
    failed={'case_id':'sample','status':'failed','error_type':'OperationalError','error':'connection failed'}
    assert entry.verified_precall_failure(tmp_path,row,failed,[],[failed])['eligible']
    assert not entry.verified_precall_failure(tmp_path,row,failed,[{'fingerprint':'unknown'}],[failed])['eligible']
    trace=tmp_path/'traces'/'sample.jsonl'
    trace.parent.mkdir()
    trace.write_text('{}',encoding='utf-8')
    assert not entry.verified_precall_failure(tmp_path,row,failed,[],[failed])['eligible']
    trace.unlink()
    cache=tmp_path/'cache'/'decision'/'response.json'
    cache.parent.mkdir(parents=True)
    entry.write_json(cache,{'user_prompt':'unique subject unique body'})
    assert not entry.verified_precall_failure(tmp_path,row,failed,[],[failed])['eligible']

def test_database_outage_stops_before_next_ticket(tmp_path,monkeypatch):
    from sqlalchemy.exc import OperationalError
    import json
    output=tmp_path/'run'
    rows=[{'case_id':f'case-{i}','input':{'subject':f's{i}','body':f'b{i}'},'query':f'q{i}'} for i in range(48)]
    monkeypatch.setattr(entry,'RUNS',tmp_path)
    monkeypatch.setattr(entry,'validate_inputs',lambda:({'snapshot_hash':'frozen'},rows,{r['case_id']:{} for r in rows}))
    monkeypatch.setattr(entry,'exact_query_vectors',lambda *a:{r['case_id']:[] for r in rows})
    attempted=[]
    def disconnected(row,*args):
        attempted.append(row['case_id'])
        raise OperationalError('SELECT 1',{},Exception('connection failed'))
    monkeypatch.setattr(entry,'run_one',disconnected)
    monkeypatch.setattr(sys,'argv',['evaluation','--output',str(output),'--execute','--cases','case-0','case-1'])
    entry.main()
    saved=json.loads((output/'results.json').read_text(encoding='utf-8'))
    assert attempted==['case-0']
    assert saved['results'][0]['pre_call_failure']
    assert 'case-1' in saved['unrun_case_ids']
    assert json.loads((output/'attempts.json').read_text(encoding='utf-8'))['attempts']==[]

@pytest.mark.integration
@pytest.mark.skipif(os.getenv('TICKETMIND_RUN_DB_TESTS')!='1',reason='opt-in real PostgreSQL/Milvus')
def test_real_api_graph_review_with_zero_model_calls(tmp_path,monkeypatch):
    from ticketmind.agent.proposals import decision_adapter
    from ticketmind.agent.semantic_judge import JudgeResult
    gate, rows, _ = entry.validate_inputs()
    qwen=entry.QwenSettings()
    vectors=entry.exact_query_vectors({r['case_id']:r['query'] for r in rows},qwen.workspace_id)
    assert len(vectors)==48
    calls={'decision':0,'judge':0}
    def decide(self,state,timeout,usage):
        calls['decision']+=1
        return decision_adapter.validate_python({'next_step':'ask_clarification','reason':'deterministic harness smoke',
                                                 'reply':'请补充未提供的脱敏错误详情。','evidence_ids':[]})
    def judge(self,state,proposal,timeout,usage):
        calls['judge']+=1
        return JudgeResult(violations=[])
    monkeypatch.setattr(entry.BudgetAdapters,'decision',decide)
    monkeypatch.setattr(entry.BudgetAdapters,'judge',judge)
    ledger=entry.CostLedger(tmp_path/'attempts.json',entry.CEILINGS)
    args=SimpleNamespace(output=tmp_path,ledger=ledger,offline_smoke=False)
    result=entry.run_one(rows[0],{},args,gate,vectors)
    assert result['status']=='succeeded',result
    assert result['run_status']=='waiting_review'
    assert result['business_row_matches_api']
    assert result['idempotency_key_replay']
    assert result['status_persisted_after_simulation']
    assert result['approval_simulation']['run_status']=='completed'
    assert calls=={'decision':1,'judge':1}
    assert ledger.data['attempts']==[]
