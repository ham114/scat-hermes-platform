"""Offline CPU BGE worker; JSON-lines only on stdout, no network or LLM calls."""
import contextlib,json,os,sys
os.environ.update(HF_HUB_OFFLINE='1',TRANSFORMERS_OFFLINE='1',HF_HUB_DISABLE_TELEMETRY='1',TOKENIZERS_PARALLELISM='false',OMP_NUM_THREADS='2')
MODEL='/mnt/h3c/scat-rag/models/embedding/bge-m3-5617a9f61b028005a4858fdac845db406aefb181'
def main():
    with contextlib.redirect_stdout(sys.stderr):
        import torch
        from sentence_transformers import SentenceTransformer
        torch.set_num_threads(2)
        model=SentenceTransformer(MODEL,device='cpu',local_files_only=True)
        model.max_seq_length=2048
    print(json.dumps({'status':'ready','model':MODEL,'device':'cpu','dimensions':1024}),flush=True)
    for line in sys.stdin:
        data=json.loads(line)
        if data.get('action')=='close':break
        with contextlib.redirect_stdout(sys.stderr):
            vectors=model.encode(data['texts'],batch_size=4,normalize_embeddings=True,convert_to_numpy=True,show_progress_bar=False).tolist()
        print(json.dumps({'id':data['id'],'vectors':vectors}),flush=True)
if __name__=='__main__':main()
