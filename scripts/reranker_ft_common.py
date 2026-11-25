"""Shared offline contracts for three independent reranker LoRA experiments."""
import hashlib,json,os,re,socket,sys,unicodedata
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'var/validation/20261125-reranker-lora-v1'
BASE_EVAL=ROOT/'var/validation/20261125-rag-holdout120-rerank-serial-v1'
sys.path[:0]=[str(ROOT/'src'),str(ROOT/'scripts')]
os.environ.update(HF_HUB_OFFLINE='1',TRANSFORMERS_OFFLINE='1',HF_HUB_DISABLE_TELEMETRY='1',TOKENIZERS_PARALLELISM='false',OMP_NUM_THREADS='4',OPENBLAS_NUM_THREADS='4',MKL_NUM_THREADS='4',WANDB_MODE='disabled',WANDB_DISABLED='true',RAGAS_DO_NOT_TRACK='true',LANGCHAIN_TRACING_V2='false',LANGSMITH_TRACING='false')
SCHEMES=[{'id':'p1_pointwise','priority':1,'objective':'pointwise'},
 {'id':'p2_multi_positive','priority':2,'objective':'multi_positive'},
 {'id':'p3_pairwise','priority':3,'objective':'pairwise'}]
SETTINGS={'seed':20261125,'epochs':1,'lora_r':8,'lora_alpha':16,'lora_dropout':.05,'target_modules':['q_proj','v_proj'],'learning_rate':2e-5,'gradient_accumulation_queries':8,'pair_microbatch':2,'max_input_tokens':2048,'negative_weight':.5,'max_grad_norm':1.,'warmup_fraction':.1}
def read(p):return json.loads(Path(p).read_text(encoding='utf8'))
def save(p,x):
 p=Path(p);p.parent.mkdir(parents=True,exist_ok=True);t=p.with_suffix(p.suffix+'.writing');t.write_text(json.dumps(x,ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf8');t.replace(p)
def sha(p):
 h=hashlib.sha256()
 with Path(p).open('rb')as f:
  for b in iter(lambda:f.read(8*1024*1024),b''):h.update(b)
 return h.hexdigest()
def norm(q):return re.sub(r'[\W_]+','',unicodedata.normalize('NFKC',q).casefold())
def offline():
 def deny(*a,**k):raise RuntimeError('network_disabled_for_local_reranker_finetuning')
 socket.socket.connect=socket.socket.connect_ex=socket.create_connection=deny
def no_generation():
 from transformers.generation.utils import GenerationMixin
 def deny(*a,**k):raise RuntimeError('text_generation_disabled_for_reranker_experiment')
 GenerationMixin.generate=deny
def corpus():
 from rag_retrieval.runtime import load_chunk_records
 cfg=read(ROOT/'services/rag/qwen3-production-v1.json');cs,rs=load_chunk_records(Path(cfg['corpus']))
 return cfg,{c.chunk_id:c for c in cs},rs
def check_protected():
 p=read(OUT/'plan.json');assert all(sha(ROOT/n)==h for n,h in p['protected_sha256'].items()),'protected inputs changed'
 assert all(sha(Path(n))==h for n,h in p.get('external_protected_sha256',{}).items()),'base model or RAG-FiT files changed'
def batch_scores(model,tokenizer,queries,documents,prefix,suffix,yes,no,*,micro=2):
 import torch
 from rag_retrieval.rerank_comparison import Qwen3Reranker
 texts=[f'<Instruct>: {Qwen3Reranker.INSTRUCTION}\n<Query>: {q}\n<Document>: {d}'for q,d in zip(queries,documents,strict=True)]
 raw=tokenizer(texts,add_special_tokens=False,padding=False,truncation=False,return_attention_mask=False)['input_ids']
 assert all(len(x)+len(prefix)+len(suffix)<=2048 for x in raw),'training_pair_would_truncate'
 packed=[prefix+x+suffix for x in raw];out=[]
 for j in range(0,len(packed),micro):
  b=tokenizer.pad({'input_ids':packed[j:j+micro]},padding=True,return_attention_mask=True,return_tensors='pt').to('cuda:0')
  values=model(**b,use_cache=False,logits_to_keep=1).logits[:,-1,[no,yes]].float()
  out.append(values[:,1]-values[:,0])
 return torch.cat(out)
def objective(scores,positive_count,kind,hard_indices=None):
 import torch
 import torch.nn.functional as F
 assert 1<=positive_count<len(scores)
 y=torch.zeros_like(scores);y[:positive_count]=1
 weights=torch.where(y>0,torch.ones_like(y),torch.full_like(y,.5))
 binary=(F.binary_cross_entropy_with_logits(scores,y,reduction='none')*weights).sum()/weights.sum()
 if kind=='pointwise':return binary
 if kind=='multi_positive':
  # Mean over EVERY positive. Logsumexp(pos) would let only the easy positive win.
  ranking=-(scores[:positive_count]-torch.logsumexp(scores,dim=0)).mean()
 elif kind=='pairwise':
  ids=hard_indices or list(range(positive_count,len(scores)))
  assert all(positive_count<=i<len(scores)for i in ids),'a positive cannot be used as a negative'
  ranking=F.softplus(.2-scores[:positive_count].min()+scores[ids]).mean()
 else:raise ValueError(kind)
 return .3*binary+.7*ranking
TOPICS={
 'weather':r'weather|cloud|ceil|dew|humidity|temperature|precipitation|rain|snow|icing|wind|visibility|meteorolog|METAR|TAF|PIREP|气象|云|露点|温度|降水|风|能见度|结冰',
 'navigation':r'\bILS\b|localizer|glide.?slope|\bVOR\b|\bNDB\b|\bDME\b|\bADF\b|\bRNAV\b|\bGPS\b|navigation|magnetic|compass|heading|bearing|radial|course|航向|导航|磁|航线',
 'surveillance':r'CAT\s*0?62|ASTERIX|transponder|Mode.?[ACS]|ADS.?B|TCAS|监视|应答机',
 'performance':r'airspeed|stall|bank angle|load factor|takeoff|landing distance|turbo|manifold|engine|fuel|weight|balance|\bCG\b|stabilit|yaw|pitch|roll|空速|失速|坡度|载荷|起飞|发动机|燃油|重量|重心|平衡|稳定|偏航',
 'airspace':r'airspace|clearance|\bATC\b|VFR|IFR|holding|runway|\bUAS\b|crew|certification|flight plan|空域|许可|管制|等待|跑道|机组|无人机|飞行计划',
 'human':r'hypox|oxygen|physiolog|vestibular|illusion|carbon monoxide|medic|drug|health|stress|headache|fatigue|situational awareness|workload|risk|judgment|缺氧|氧气|生理|前庭|错觉|一氧化碳|药|健康|压力|头痛|疲劳|情境意识|风险',
 'instruments':r'gyroscop|pitot|altimeter|electrical|attitude|horizon|indicator|陀螺|皮托|高度表|电气|姿态|地平线',
 'altimetry':r'altitude|height|elevation|flight level|\bFL\b|高度|标高|高程',
 'communications':r'frequency|\bVHF\b|\bUHF\b|\bHF\b|\bVLF\b|communication|radio|频率|频段|通信|无线电'}
def tags(text):return {k for k,p in TOPICS.items()if re.search(p,text,re.I)}
def negative_allowed(*,candidate,protected_ids,protected_hashes,positive_sections,query_tags,candidate_tags,positive_similarity):
 if candidate['chunk_id']in protected_ids or candidate['text_sha256']in protected_hashes:return False
 if not candidate_tags or not query_tags or candidate_tags&query_tags:return False
 if (candidate['source_id'],tuple(candidate['section_path']))in positive_sections:return False
 if positive_similarity>=.8 or '$$'in candidate['text']:return False
 return True
