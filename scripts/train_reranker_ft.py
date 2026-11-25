"""LoRA updates of yes/no relevance logits only; no answer generation or API calls."""
import argparse,gc,math,random,time
from reranker_ft_common import *

def load_scorer(adapter=None):
 no_generation()
 from rag_retrieval.rerank_comparison import Qwen3Reranker
 cfg=read(ROOT/'services/rag/qwen3-production-v1.json');m=read(ROOT/cfg['model_manifest'])
 for row in m['files']:assert sha(Path(m['destination'])/row['path'])==row['sha256'],'base model changed'
 r=Qwen3Reranker(m['destination'],batch_size=4,max_length=2048)
 if adapter:
  from peft import PeftModel
  a=Path(adapter).resolve();assert a.is_relative_to(OUT.resolve())
  r.model=PeftModel.from_pretrained(r.model,str(a),is_trainable=False).eval()
 return r

def main():
 p=argparse.ArgumentParser();p.add_argument('--scheme',choices=[s['id']for s in SCHEMES]);p.add_argument('--smoke',action='store_true');p.add_argument('--smoke-tag',default='smoke');a=p.parse_args()
 offline();import torch
 from peft import LoraConfig,get_peft_model
 if not a.smoke:check_protected()
 else:
  draft=read(OUT/'draft-plan.json');assert all(sha(ROOT/n)==h for n,h in draft['protected_sha256'].items())
 random.seed(SETTINGS['seed']);torch.manual_seed(SETTINGS['seed']);torch.cuda.manual_seed_all(SETTINGS['seed']);torch.set_num_threads(4)
 name=a.smoke_tag if a.smoke else a.scheme
 if a.smoke:assert __import__('re').fullmatch(r'smoke(?:-v\d+)?',name)
 dest=OUT/name
 assert not (dest/'training.json').exists(),'completed training is immutable'
 dest.mkdir(parents=True,exist_ok=True)
 train=[json.loads(l)for l in (OUT/'train.jsonl').read_text(encoding='utf8').splitlines()]
 cfg,chunks,rs=corpus();r=load_scorer()
 config=LoraConfig(task_type='CAUSAL_LM',r=SETTINGS['lora_r'],lora_alpha=SETTINGS['lora_alpha'],lora_dropout=SETTINGS['lora_dropout'],target_modules=SETTINGS['target_modules'],bias='none')
 r.model=get_peft_model(r.model,config);r.model.config.use_cache=False;r.model.gradient_checkpointing_enable(gradient_checkpointing_kwargs={'use_reentrant':False});r.model.enable_input_require_grads()
 for n,param in r.model.named_parameters():
  if param.requires_grad:
   assert 'lora_'in n,'base parameter unexpectedly trainable'
   if param.dtype!=torch.float32:param.data=param.data.float()
 params=[p for p in r.model.parameters()if p.requires_grad];count=sum(p.numel()for p in params)
 assert 0<count<3000000
 optim=torch.optim.AdamW(params,lr=SETTINGS['learning_rate'],weight_decay=.01)
 scaler=torch.amp.GradScaler('cuda',init_scale=128.)
 rng=random.Random(SETTINGS['seed']);rng.shuffle(train)
 if a.smoke:train=train[:1]
 total=math.ceil(len(train)/SETTINGS['gradient_accumulation_queries']);warm=max(1,math.ceil(total*.1))
 scheduler=torch.optim.lr_scheduler.LambdaLR(optim,lambda step:min((step+1)/warm,max(0.,(total-step)/max(1,total-warm))))
 kind='pointwise'if a.smoke else next(x['objective']for x in SCHEMES if x['id']==a.scheme)
 r.model.train();losses=[];gradient_norms=[];began=time.time();step=0
 for offset in range(0,len(train),SETTINGS['gradient_accumulation_queries']):
  group_batch=train[offset:offset+SETTINGS['gradient_accumulation_queries']];optim.zero_grad(set_to_none=True)
  for group in group_batch:
   cids=group['gold']+[n['chunk_id']for n in group['negatives']];docs=[chunks[c].retrieval_text()for c in cids]
   scores=batch_scores(r.model,r.tokenizer,[group['question']]*len(cids),docs,r.prefix,r.suffix,r.yes,r.no,micro=SETTINGS['pair_microbatch'])
   loss=objective(scores,len(group['gold']),kind,group['hard_negative_positions'])
   assert torch.isfinite(loss),'nonfinite training loss'
   if a.smoke:
    assert all(torch.isfinite(objective(scores,len(group['gold']),k,group['hard_negative_positions']))for k in ['pointwise','multi_positive','pairwise'])
   losses.append(float(loss.detach()));scaler.scale(loss/len(group_batch)).backward()
  scaler.unscale_(optim);grad=torch.nn.utils.clip_grad_norm_(params,SETTINGS['max_grad_norm'],error_if_nonfinite=True)
  assert torch.isfinite(grad) and float(grad)>0,'no finite LoRA gradient'
  gradient_norms.append(float(grad));scaler.step(optim);scaler.update();scheduler.step();step+=1
  save(dest/'progress.json',{'stage':'training','scheme':name,'optimizer_steps':step,'total_steps':total,'queries_seen':min(offset+len(group_batch),len(train)),'queries':len(train),'last_loss':losses[-1]})
  if step%5==0:print(json.dumps({'scheme':name,'optimizer_step':step,'total_steps':total,'loss':losses[-1]}),flush=True)
 target=dest/'adapter-last';assert target.resolve().is_relative_to(OUT.resolve())
 r.model.save_pretrained(str(target),safe_serialization=True)
 adapter_files={f.name:sha(f)for f in target.iterdir()if f.is_file()}
 smoke={}
 if a.smoke:
  group=train[0];sample=[chunks[group['gold'][0]].retrieval_text(),chunks[group['negatives'][0]['chunk_id']].retrieval_text()]
  r.model.eval();before,_=r.score(group['question'],sample)
  r2=load_scorer(target);after,_=r2.score(group['question'],sample)
  assert max(abs(a-b)for a,b in zip(before,after))<.001,'adapter reload score mismatch'
  smoke={'all_three_losses_finite':True,'adapter_reload_score_max_abs_delta':max(abs(a-b)for a,b in zip(before,after)),'lora_only_gradients':True,'base_weight_files_unchanged':True}
 else:check_protected()
 result={'status':'completed','scheme':name,'objective':kind,'settings':SETTINGS,'train_queries':len(train),'optimizer_steps':step,'trainable_parameters':count,'loss_mean':sum(losses)/len(losses),'first_loss':losses[0],'last_loss':losses[-1],'gradient_norm_max':max(gradient_norms),'seconds':time.time()-began,'adapter_files_sha256':adapter_files,'adapter_path':str(target),'smoke':smoke,'external_llm_calls':0,'generation_calls':0,'production_changes':0}
 save(dest/'training.json',result);print(json.dumps(result),flush=True)
if __name__=='__main__':main()
