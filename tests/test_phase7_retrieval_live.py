"""Deterministic scoring and cache guards; no external requests."""
import importlib.util
from pathlib import Path
import sys
import pytest

SCRIPTS=Path(__file__).resolve().parents[1]/'scripts'
sys.path.insert(0,str(SCRIPTS))
spec=importlib.util.spec_from_file_location('p7retrieval',SCRIPTS/'evaluate_phase7_retrieval.py')
module=importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

def test_failed_queries_remain_in_denominator_and_docs_prefix_matches():
    rows=[{'kind':'docs','mode':'dense','case_id':'one','relevant_ids':['a','b'],
           'hits':[{'source_id':'docs:a'}],'status':'succeeded','duration_ms':2},
          {'kind':'docs','mode':'dense','case_id':'two','relevant_ids':['a'],
           'hits':[],'status':'failed','duration_ms':4},
          {'kind':'docs','mode':'dense','case_id':'open','relevant_ids':[],
           'hits':[{'source_id':'docs:b'}],'status':'succeeded','duration_ms':3}]
    result=module.score(rows,'docs','dense',5)
    assert result['denominator']==2 and result['query_count']==3
    assert result['failures']==1 and result['hit_at_k']==.5
    assert result['recall_at_k']==.25 and result['mrr_at_k']==.5
    assert len(result['no_relevant_queries'])==1

def test_invalid_vectors_and_unmatched_query_rejected():
    for vectors in ([[0]*1024],[[1]*1023],[[float('nan')]+[1]*1023]):
        with pytest.raises(ValueError): module.validate_vectors(vectors,1)
    with pytest.raises(ValueError): module.CachedQuery('exact',[1]).embed_query('different')
