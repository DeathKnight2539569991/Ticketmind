"""Bounded real retrieval evaluation; no Decision/Judge or production DB writes."""
from __future__ import annotations
import argparse
import hashlib
import json
import math
from pathlib import Path
import statistics
import sys
from time import perf_counter as monotonic
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from phase7_offline import DATA, check_frozen, export_reviewed_queries, jsonl
from ticketmind.agent.dev_acceptance import AttemptLedger, acceptance_lock, write_json
from ticketmind.core.config import QwenSettings, MilvusSettings, ProcessingSettings
from ticketmind.documents.markdown import parse_markdown
from ticketmind.documents.index import docs_manifest, docs_collection, MilvusDocsIndex
from ticketmind.documents.service import retrieve_docs
from ticketmind.knowledge.sources import load_sources
from ticketmind.knowledge.corpus import build_case_text
from ticketmind.retrieval.milvus_client import build_milvus_client
from ticketmind.retrieval.versioned_collection import create_versioned_collection
from ticketmind.retrieval.service import retrieve_cases
from ticketmind.retrieval.schemas import DocEvidenceHit
from ticketmind.retrieval.transport import SingleRequestSession

MODEL = 'text-embedding-v4'
RATE = .5 / 1_000_000

def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()

def validate_vectors(vectors, count):
    if len(vectors) != count or any(len(v) != 1024 or not all(math.isfinite(x) for x in v)
                                  or not any(v) for v in vectors):
        raise ValueError('invalid_embedding_vectors')

def score(rows, kind, mode, k):
    selected = [r for r in rows if r['kind'] == kind and r['mode'] == mode]
    positive = [r for r in selected if r['relevant_ids']]
    def values(r):
        ids = [h['source_id'].removeprefix('docs:') for h in r['hits'][:k]]
        rel = set(r['relevant_ids'])
        return bool(rel.intersection(ids)), len(rel.intersection(ids))/len(rel), next(
            (1/i for i,s in enumerate(ids,1) if s in rel),0)
    n = len(positive)
    vals = [values(r) for r in positive]
    return {'denominator':n,'query_count':len(selected),'failures':sum(r['status']!='succeeded' for r in selected),
            'hit_at_k':sum(v[0] for v in vals)/n if n else None,
            'recall_at_k':sum(v[1] for v in vals)/n if n else None,
            'mrr_at_k':sum(v[2] for v in vals)/n if n else None,
            'median_ms':statistics.median(r['duration_ms'] for r in selected) if selected else None,
            'no_relevant_queries':[{'case_id':r['case_id'],'returned_ids':[h['source_id'] for h in r['hits']]}
                                   for r in selected if not r['relevant_ids']]}

class CachedQuery:
    def __init__(self, query, vector): self.query,self.vector=query,vector
    def embed_query(self, query):
        if query != self.query: raise ValueError('query_cache_mismatch')
        return self.vector

class SnapshotDocs:
    """Frozen-file hydration adapter; production BM25 index/service, no DB claim."""
    def __init__(self, version, chunks):
        self.version,self.chunks=version,chunks
        self.meta=SimpleNamespace(version=version, collection_name=docs_collection(version), manifest=docs_manifest(version))
    def dataset(self): return self.meta
    def hydrate(self, rows, *, mode, rank_start=1, record=None):
        result=[]
        for r in rows:
            e=r['entity']; cid=e['source_id'].removeprefix('docs:'); c=self.chunks[cid]
            if e['docs_version'] != self.version or e['content_hash'] != c.content_hash:
                raise ValueError('docs_snapshot_mismatch')
            result.append(c.model_copy(update={'score':float(r['distance']), 'mode':mode,'rank':rank_start+len(result)}))
        return result

def docs_dense(client, name, query, vector, chunks, mode):
    channels={}
    for channel in (('dense','bm25') if mode=='hybrid' else (mode,)):
        rows=client.search(collection_name=name,data=[vector if channel=='dense' else query.lower()],
            anns_field='embedding' if channel=='dense' else 'sparse',
            search_params={'metric_type':'COSINE' if channel=='dense' else 'BM25'},
            output_fields=['source_id','content_hash'],limit=20,consistency_level='Strong',timeout=10)[0]
        channels[channel]=[]
        for row in rows:
            sid=row['entity']['source_id']; c=chunks[sid.removeprefix('docs:')]
            if row['entity']['content_hash']!=c.content_hash: raise ValueError('docs_hash_mismatch')
            channels[channel].append((sid,float(row['distance'])))
    if mode=='hybrid':
        fused={}
        for hits in channels.values():
            for i,(sid,_) in enumerate(hits,1): fused[sid]=fused.get(sid,0)+1/(60+i)
        hits=sorted(fused.items(),key=lambda x:(-x[1],x[0]))
    else: hits=channels[mode]
    return [{'source_id':sid,'score':value} for sid,value in hits[:5]]

def ensure_docs_experiment(client, name, description, analyzer):
    from pymilvus import DataType, Function, FunctionType
    if client.has_collection(collection_name=name,timeout=10):
        if client.describe_collection(collection_name=name,timeout=10)['description']!=description:
            raise ValueError('experimental_collection_conflict')
        return
    s=client.create_schema(auto_id=False,enable_dynamic_field=False,description=description)
    s.add_field('source_id',DataType.VARCHAR,is_primary=True,max_length=128)
    s.add_field('content_hash',DataType.VARCHAR,max_length=64)
    s.add_field('bm25_text',DataType.VARCHAR,max_length=8192,enable_analyzer=True,analyzer_params=analyzer)
    s.add_field('embedding',DataType.FLOAT_VECTOR,dim=1024)
    s.add_field('sparse',DataType.SPARSE_FLOAT_VECTOR)
    s.add_function(Function(name='docs_bm25',input_field_names=['bm25_text'],output_field_names=['sparse'],function_type=FunctionType.BM25))
    ix=client.prepare_index_params()
    ix.add_index(field_name='embedding',index_name='embedding_flat',index_type='FLAT',metric_type='COSINE')
    ix.add_index(field_name='sparse',index_name='sparse_bm25',index_type='SPARSE_INVERTED_INDEX',metric_type='BM25',params={'bm25_k1':1.2,'bm25_b':.75})
    client.create_collection(collection_name=name,schema=s,index_params=ix,consistency_level='Strong',timeout=10)

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute',action='store_true')
    parser.add_argument('--cached-only',action='store_true',help='Fail before any embedding request if cache is missing')
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args(); out=args.output.resolve()
    parent=(ROOT.parent/'log/evaluation').resolve()
    if not out.is_relative_to(parent) or out==parent: parser.error('invalid_output')
    gate=check_frozen(require_reviewed=True)
    qwen=QwenSettings().model_copy(update={'embedding_model':MODEL})
    corpus=load_sources(DATA/'historical_cases.jsonl')
    labels={r['case_id']:r for r in jsonl(DATA/'evaluation_labels.jsonl')}
    queries=jsonl(DATA/'case_retrieval_queries.jsonl'); chunks={}; doctexts={}
    for path in sorted((DATA/'docs').glob('*.md')):
        doc=parse_markdown(path.stem,path.read_text(encoding='utf-8'))
        for ch in doc.chunks:
            c=DocEvidenceHit(source_id='docs:'+ch.chunk_id,doc_id=doc.doc_id,chunk_id=ch.chunk_id,
                title=doc.title,section=ch.section,text=ch.text,content_hash=ch.content_hash,
                score=0,docs_version='phase7-retrieval-'+gate['snapshot_hash'][:16],synthetic=True,mode='bm25',rank=1)
            chunks[ch.chunk_id]=c; doctexts[c.source_id]=f'{doc.title}\n{ch.section}\n{ch.text}'
    groups=[('document',[(sid,build_case_text(c)) for sid,c in sorted(corpus.cases.items())]),
            ('document',sorted(doctexts.items())),('query',[(q['case_id'],q['query']) for q in queries])]
    byte_bound=sum(len(t.encode()) for _,items in groups for _,t in items)
    assert sum(len(items) for _,items in groups)==192
    if byte_bound*RATE>.10: raise ValueError('cost_bound_exceeded')
    config={'snapshot':gate['snapshot_hash'],'model':MODEL,'dimension':1024,'workspace_hash':digest(qwen.workspace_id),
            'milvus_hash':digest(MilvusSettings().uri),'texts':192,'maximum_http_attempts':21,'budget_cny':.10,
            'utf8_byte_token_upper_estimate':byte_bound,'max_estimated_cny':byte_bound*RATE,
            'top_k':5,'candidate_k':20,'rrf_k':60,'docs_dense':'experimental_only','automatic_retries':0}
    print(json.dumps(config),flush=True)
    if not args.execute: return
    out.mkdir(parents=True,exist_ok=True)
    if (out/'config.json').exists() and json.loads((out/'config.json').read_text())!=config:
        raise ValueError('config_conflict')
    write_json(out/'config.json',config)
    with acceptance_lock(out):
        export_reviewed_queries(out/'reviewed_queries.jsonl')
        client=build_milvus_client(MilvusSettings())
        try:
            version=client.get_server_version(timeout=10)
            name=create_versioned_collection(client,corpus,timeout=10)
            ds=SnapshotDocs(next(iter(chunks.values())).docs_version,chunks)
            idx=MilvusDocsIndex(client); idx.ensure(ds.meta)
            exp='phase7_docs_experiment_'+digest({'snapshot':gate['snapshot_hash'],'model':MODEL,'text':doctexts})[:24]
            ensure_docs_experiment(client,exp,'phase7_docs_experiment:'+gate['snapshot_hash'],ds.meta.manifest['analyzer'])
            ledger=AttemptLedger(out/'attempts.json',{'initial_embedding':21,'research_embedding':0,'decision':0})
            vectors={}
            import dashscope
            dashscope.base_http_api_url=f'https://{qwen.workspace_id}.cn-beijing.maas.aliyuncs.com/api/v1'
            for typ,items in groups:
                for offset in range(0,len(items),10):
                    batch=items[offset:offset+10]
                    fp=digest({'workspace':config['workspace_hash'],'model':MODEL,'dimension':1024,'type':typ,'items':batch})
                    cache=out/'vectors'/f'{fp}.json'
                    if cache.exists():
                        data=json.loads(cache.read_text(encoding='utf-8'))
                        if data['fingerprint']!=fp: raise ValueError('cache_conflict')
                        vv=data['vectors']
                    else:
                        if args.cached_only: raise ValueError('embedding_cache_missing_no_calls_allowed')
                        def call(record):
                            with SingleRequestSession() as session:
                                response=dashscope.TextEmbedding.call(model=MODEL,input=[t for _,t in batch],
                                    text_type=typ,dimension=1024,api_key=qwen.api_key.get_secret_value(),
                                    request_timeout=30,session=session)
                            record['usage']=response.get('usage');record['request_id']=response.get('request_id')
                            if response.status_code!=200: raise RuntimeError('embedding_'+str(response.status_code)+'_'+str(response.get('code')))
                            embeddings=sorted(response.output['embeddings'],key=lambda r:r['text_index'])
                            if [r['text_index'] for r in embeddings]!=list(range(len(batch))):
                                raise ValueError('embedding_indices_mismatch')
                            vv=[r['embedding'] for r in embeddings];validate_vectors(vv,len(batch))
                            write_json(cache,{'fingerprint':fp,'ids':[sid for sid,_ in batch],'vectors':vv,'usage':record['usage']})
                            return vv
                        vv=ledger.attempt('initial_embedding',fp,call)
                    validate_vectors(vv,len(batch));vectors.update({sid:v for (sid,_),v in zip(batch,vv)})
                    print(json.dumps({'stage':'embedding','type':typ,'completed_in_group':offset+len(batch)}),flush=True)
            client.upsert(collection_name=name,data=[{'source_id':sid,'text':build_case_text(c),
                'bm25_text':build_case_text(c).lower(),'corpus_version':corpus.version,'embedding':vectors[sid]}
                for sid,c in corpus.cases.items()],timeout=30)
            for c in chunks.values():
                idx.upsert(ds.meta,SimpleNamespace(title=c.title),c)
            client.upsert(collection_name=exp,data=[{'source_id':c.source_id,'content_hash':c.content_hash,
                'bm25_text':doctexts[c.source_id].lower(),'embedding':vectors[c.source_id]} for c in chunks.values()],timeout=30)
            for collection,count in ((name,120),(ds.meta.collection_name,24),(exp,24)):
                actual=client.query(collection_name=collection,filter='',output_fields=['count(*)'],consistency_level='Strong',timeout=10)[0]['count(*)']
                if actual!=count: raise ValueError('collection_count_mismatch')
            rows=[]; pc=ProcessingSettings(retrieval_top_k=5,retrieval_candidate_k=20,retrieval_rrf_k=60)
            for q in queries:
                for kind in ('cases','docs'):
                    for mode in ('bm25','dense','hybrid'):
                        started=monotonic(); row={'case_id':q['case_id'],'kind':kind,'mode':mode,
                            'relevant_ids':labels[q['case_id']]['relevant_source_ids' if kind=='cases' else 'relevant_doc_chunk_ids'],
                            'hits':[],'status':'failed','implementation':'production_retrieval_with_frozen_adapter' if kind=='cases' or mode=='bm25' else 'isolated_docs_experiment'}
                        try:
                            if kind=='cases':
                                hits=retrieve_cases(q['query'],client=client,embeddings=CachedQuery(q['query'],vectors[q['case_id']]),
                                    corpus=corpus,config=pc.model_copy(update={'retrieval_mode':mode}),timeout=10,record={})
                                row['hits']=[h.model_dump(mode='json') for h in hits]
                            elif mode=='bm25':
                                row['hits']=[h.model_dump(mode='json') for h in retrieve_docs(q['query'],client=client,store=ds,mode='bm25',top_k=5)]
                            else: row['hits']=docs_dense(client,exp,q['query'],vectors[q['case_id']],chunks,mode)
                            row['status']='succeeded'
                        except Exception as exc: row['error']=getattr(exc,'code',type(exc).__name__)
                        row['duration_ms']=round((monotonic()-started)*1000,3);rows.append(row)
                        with (out/'trace.jsonl').open('a',encoding='utf-8') as h: h.write(json.dumps(row,ensure_ascii=False)+'\n')
                print(json.dumps({'stage':'retrieval','case_id':q['case_id']}),flush=True)
            token_usage=sum((a.get('usage') or {}).get('total_tokens',0) for a in ledger.data['attempts'])
            report={'config':config,'server_version':version,'collections':{'cases':name,'docs_bm25':ds.meta.collection_name,'docs_experiment':exp},
                'embedding_attempts':len(ledger.data['attempts']),'embedding_tokens':token_usage,'estimated_cny':token_usage*RATE,
                'metrics':{kind:{mode:{str(k):score(rows,kind,mode,k) for k in (1,3,5)} for mode in ('bm25','dense','hybrid')} for kind in ('cases','docs')},'rows':rows}
            write_json(out/'results.json',report)
            print(json.dumps({k:v for k,v in report.items() if k!='rows'},ensure_ascii=False),flush=True)
        finally: client.close()

if __name__=='__main__': main()
