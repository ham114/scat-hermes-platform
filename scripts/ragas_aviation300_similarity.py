"""Local multilingual duplicate diagnostics; never send old questions to an API."""
import hashlib,json,os,sys
from pathlib import Path
os.environ.update(HF_HUB_OFFLINE='1',TRANSFORMERS_OFFLINE='1',TOKENIZERS_PARALLELISM='false',OMP_NUM_THREADS='4')
ROOT=Path(__file__).resolve().parents[1]
P=ROOT.parent/'scat-hermes-platform';OUT=ROOT/'reports/batch-20261124-aviation300'

def main():
    import numpy as np
    from sentence_transformers import SentenceTransformer
    state=json.loads((OUT/'state.json').read_text());cases=sorted(state['accepted'],key=lambda c:c['slot'])
    if len(cases)!=300:raise ValueError('Wait for 300 candidates')
    old=json.loads((OUT/'old-question-fingerprints.json').read_text())
    model=SentenceTransformer('/mnt/h3c/scat-rag/models/embedding/bge-m3-5617a9f61b028005a4858fdac845db406aefb181',device='cuda',local_files_only=True)
    vectors=model.encode([c['question'] for c in cases]+[x['question'] for x in old],batch_size=8,normalize_embeddings=True,convert_to_numpy=True,show_progress_bar=False)
    questions=vectors[:300];similarity=questions@questions.T
    pairs=[]
    for i in range(300):
        for j in range(i+1,300):
            if float(similarity[i,j])>=.87:
                a,b=cases[i],cases[j]
                pairs.append({'left':a['qid'],'right':b['qid'],'cosine':float(similarity[i,j]),'left_question':a['question'],'right_question':b['question'],'left_reference':a['reference'],'right_reference':b['reference']})
    previous=questions@vectors[300:].T;old_pairs=[]
    for i,c in enumerate(cases):
        j=int(previous[i].argmax());score=float(previous[i,j])
        if score>=.85:old_pairs.append({'qid':c['qid'],'cosine':score,'question':c['question'],'prior_question':old[j]['question'],'note':'Topic-overlap flag only; differing source, scope or required fact may make this a valid new question'})
    result={'method':'local pinned BGE-M3 question similarity; flags are not automatic duplicate judgments','candidates':300,
        'within_batch_threshold':.87,'within_batch_pairs':sorted(pairs,key=lambda x:-x['cosine']),
        'previous_topic_threshold':.85,'previous_topic_flags':sorted(old_pairs,key=lambda x:-x['cosine']),
        'external_model_calls':0,'old_questions_left_server':False,'candidates_sha256':hashlib.sha256((OUT/'candidates.jsonl').read_bytes()).hexdigest()}
    (OUT/'similarity-audit.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    np.save(OUT/'question-vectors.npy',questions)
    print(json.dumps({'within_batch_pairs':len(pairs),'prior_topic_flags':len(old_pairs),'external_model_calls':0}))

if __name__=='__main__':main()
