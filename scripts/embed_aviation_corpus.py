"""Embed only new official chunks with the installed pinned BGE-M3 model."""
from __future__ import annotations
import hashlib,json,os,sys,time
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
TARGET=ROOT/'var/rag/corpora/kg_20261123_aviation_v5'
OLD=ROOT/'var/rag/corpora/kg_20261026_research_v4'
os.environ.update(HF_HUB_OFFLINE='1',TRANSFORMERS_OFFLINE='1',TOKENIZERS_PARALLELISM='false',OMP_NUM_THREADS='4')

def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()
def read(path):return json.loads(path.read_text())
def save(path,value):
    tmp=path.with_suffix(path.suffix+'.writing');tmp.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n');tmp.replace(path)

def main():
    import numpy as np
    import torch
    from sentence_transformers import SentenceTransformer
    from rag_retrieval.runtime import load_chunk_records
    if (TARGET/'ready.json').exists():raise ValueError('Immutable generation already ready')
    config=read(ROOT/'services/rag/qwen3-production-v1.json')
    assert config['generation_id']=='kg_20261026_research_v4'
    assert sha(OLD/'bge-m3.npy')==config['embedding_cache_sha256']
    path=TARGET/'rag_chunks.v1.jsonl';digest=sha(path)
    chunks,records=load_chunk_records(path)
    old_chunks,_=load_chunk_records(OLD/'rag_chunks.v1.jsonl')
    assert [r.chunk_id for r in chunks[:149]]==[r.chunk_id for r in old_chunks]
    parent=np.load(OLD/'bge-m3.npy',mmap_mode='r');assert parent.shape==(149,1024)
    new=chunks[149:];partial=TARGET/'bge-m3.new.partial.npy';state=TARGET/'embedding-progress.json'
    if state.exists():
        progress=read(state);assert progress['corpus_sha256']==digest
        vectors=np.lib.format.open_memmap(partial,mode='r+')
        start=progress['completed_rows']
    else:
        vectors=np.lib.format.open_memmap(partial,mode='w+',dtype='float32',shape=(len(new),1024));start=0
    assert torch.cuda.is_available()
    model=SentenceTransformer(config['embedding_model'],device='cuda',local_files_only=True)
    started=time.time()
    for index in range(start,len(new),128):
        batch=new[index:index+128]
        vectors[index:index+len(batch)]=model.encode([c.retrieval_text() for c in batch],batch_size=8,normalize_embeddings=True,convert_to_numpy=True,show_progress_bar=False)
        vectors.flush()
        progress={'corpus_sha256':digest,'completed_rows':index+len(batch),'new_rows':len(new),'model':config['embedding_model'],'batch_size':8,'external_model_calls':0}
        save(state,progress);print(json.dumps(progress),flush=True)
    combined=np.concatenate([np.asarray(parent),np.asarray(vectors)])
    assert combined.shape==(len(chunks),1024) and np.isfinite(combined).all()
    assert np.allclose(np.linalg.norm(combined,axis=1),1,atol=1e-3)
    assert np.array_equal(combined[:149],parent)
    cache=TARGET/'bge-m3.npy';np.save(cache,combined)
    candidate={**config,'generation_id':TARGET.name,'corpus':str(path),'corpus_sha256':digest,
        'chunk_count':len(chunks),'embedding_cache':str(cache),'embedding_cache_sha256':sha(cache)}
    native=read(ROOT/'services/rag/real-agent-production-v1.json')
    native.update(scope='production-knowledge-aviation-v5',generation_id=TARGET.name,corpus=str(path),corpus_sha256=digest,corpus_count=len(chunks))
    save(TARGET/'retrieval-config.json',candidate);save(TARGET/'real-agent-config.json',native)
    save(ROOT/'services/rag/qwen3-aviation-v5.json',candidate);save(ROOT/'services/rag/real-agent-aviation-v5.json',native)
    ready={'status':'ready-for-retrieval-validation','generation_id':TARGET.name,'corpus_sha256':digest,
        'chunk_count':len(chunks),'new_chunks':len(new),'parent_records_and_vectors_unchanged':149,
        'embedding_model':config['embedding_model'],'embedding_cache_sha256':sha(cache),'embedding_seconds':time.time()-started,
        'external_model_calls':0,'production_switched':False}
    save(TARGET/'ready.json',ready);print(json.dumps(ready),flush=True)

if __name__=='__main__':main()
