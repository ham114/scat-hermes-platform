"""Build a new traceable corpus; preserve all v4 records and their identities."""
from __future__ import annotations
import argparse
from collections import Counter
from datetime import datetime,timezone
import hashlib,json,os,re,sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
BATCH=ROOT/'var/rag/sources/official-aviation-20261123'
OLD=ROOT/'var/rag/corpora/kg_20261026_research_v4'
TARGET=ROOT/'var/rag/corpora/kg_20261123_aviation_v5'
MODEL='/mnt/h3c/scat-rag/models/embedding/bge-m3-5617a9f61b028005a4858fdac845db406aefb181'
os.environ.update(HF_HUB_OFFLINE='1',TRANSFORMERS_OFFLINE='1',TOKENIZERS_PARALLELISM='false')
ALIASES={
 'eurocontrol-asterix-cat062':'EUROCONTROL ASTERIX CAT062 航空监视航迹数据交换规范',
 'faa-phak':'FAA 飞行员航空知识手册 Pilot Handbook of Aeronautical Knowledge',
 'faa-pilot-controller-glossary':'FAA 飞行员管制员术语表 Pilot Controller Glossary',
 'faa-instrument-flying-handbook':'FAA 仪表飞行手册 Instrument Flying Handbook',
 'faa-aviation-weather-handbook':'FAA 航空气象手册 Aviation Weather Handbook',
 'faa-aim':'FAA 航空情报手册 Aeronautical Information Manual',
 'wmo-no8-volume1':'WMO 第8号仪器与观测方法指南 第一卷 气象变量测量',
 'ecmwf-era5-documentation':'ECMWF ERA5 数据说明 再分析气象数据文档',
}

def sha(data):return hashlib.sha256(data).hexdigest()
def read(path):return json.loads(path.read_text(encoding='utf8'))
def save(path,value):path.write_text(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf8')

def generation_metadata(generation,audit):
    parent=read(OLD/'rag_generation.v1.json')
    policy={'retained_parent_policy':parent['chunking_config'],'external_official':{
        'chunker':'Docling HybridChunker','max_tokens':640,'tokenizer':'pinned BGE-M3',
        'repeat_table_header':True,'merge_peers':True,'pdf_batch_overlap_pages':2,
        'formula_restoration':'exact native orig text only; no LLM transcription'}}
    generation.update(source_count=len(generation['sources']),status='built-pending-retrieval-validation',
        tokenizer_id='mixed: retained parent tokenizer; pinned BGE-M3 for external chunks',
        chunking_config=policy,chunking_config_sha256=sha(json.dumps(policy,sort_keys=True).encode()),
        quality={'parent_quality':parent['quality'],'new_chunks':audit['new_chunks'],
            'native_formula_text_restored':len(audit['restored_native_formula_text']),
            'unresolved_formulas':len(audit['unresolved_formulas']),
            'quarantined_chunks':len(audit['quarantined_chunks']),
            'independent_audit':'artifact-audit.json; mechanical checks, not semantic accuracy'},
        notes=['All 149 v4 records remain byte-for-byte unchanged; eight official source families appended.',
            'Original PDF/HTML snapshots, SHA256, editions and jurisdiction are retained.',
            'FAA AIM and glossary use same-edition official HTML; complete PDFs remain archived.',
            'Figures are not interpreted; native formula text preserves spatial-notation limitations.',
            'No external LLM calls, model fine-tuning, or old evaluation-label changes.'])
    return generation

def components(pilot):
    if not pilot:return read(BATCH/'extraction-manifest.json')['extractions']
    r=read(BATCH/'originals/asterix-cat062-ed1-21.pdf.metadata.json')
    return [{**r,'extraction_path':'extracted/eurocontrol-asterix-cat062','page_range':[1,146]}]

def make_rows(entries):
    from docling_core.types.doc import DoclingDocument
    from docling_core.transforms.chunker.hybrid_chunker import HybridChunker
    from docling_core.transforms.chunker.tokenizer.huggingface import HuggingFaceTokenizer
    from transformers import AutoTokenizer
    tokenizer=AutoTokenizer.from_pretrained(MODEL,local_files_only=True)
    chunker=HybridChunker(tokenizer=HuggingFaceTokenizer(tokenizer=tokenizer,max_tokens=640),merge_peers=True,repeat_table_header=True)
    rows=[];restored=[];unresolved=[];skipped=[];seen=set();stats=[]
    for entry in entries:
        folder=BATCH/entry['extraction_path'];original=BATCH/entry['relative_path']
        assert sha(original.read_bytes())==entry['sha256']
        doc=DoclingDocument.load_from_json(folder/'document.json')
        # Formula enrichment is off. Docling sometimes stores native PDF text
        # in orig while leaving text empty. Restore only that exact native text.
        for item in doc.texts:
            if item.label.value!='formula':continue
            if not item.text.strip() and item.orig.strip():
                item.text=item.orig
                restored.append({'source_id':entry['source_id'],'component':entry['filename'],
                    'extraction':entry['extraction_path'],'doc_item':item.self_ref,
                    'pages':[p.page_no for p in item.prov],'native_text':item.orig})
            elif not item.text.strip():unresolved.append({'source_id':entry['source_id'],'component':entry['filename'],'doc_item':item.self_ref,'pages':[p.page_no for p in item.prov]})
        # The new JSON is the exact normalized artifact cited by new chunks.
        normalized=folder/'document.rag.json';doc.save_as_json(normalized)
        doc.save_as_markdown(folder/'document.rag.md')
        doc_digest=sha(normalized.read_bytes());added=0
        for index,chunk in enumerate(chunker.chunk(doc)):
            body=chunk.text.strip()
            if not body or re.fullmatch(r'(This page (is )?intentionally left blank[.\s]*)+',body,re.I):continue
            dedup=(entry['source_id'],entry['sha256'],re.sub(r'\s+',' ',body).strip())
            if dedup in seen:continue
            seen.add(dedup)
            meta=chunk.meta.model_dump(mode='json')
            items=meta.get('doc_items',[])
            pages=sorted({p['page_no'] for item in items for p in item.get('prov',[])})
            headings=list(dict.fromkeys([ALIASES[entry['source_id']],*(meta.get('headings') or [])]))
            text=f"{entry['institution']} | {entry['edition']} | {entry['jurisdiction']}\n"+chunker.contextualize(chunk=chunk)
            if '<!-- formula-not-decoded -->' in text:
                text=text.replace('<!-- formula-not-decoded -->','[Formula not extracted; consult original page]')
            count=len(tokenizer.encode(text,add_special_tokens=False))
            if count>1800:
                skipped.append({'source_id':entry['source_id'],'component':entry['filename'],'chunk_index':index,'reason':'context exceeds reranker budget; retained in normalized source','tokens':count})
                continue
            locator={'kind':'docling','document_json':str(normalized.relative_to(ROOT)),
                'document_json_sha256':doc_digest,'source_url':entry['url'],
                'component':entry['filename'],'doc_item_refs':[i['self_ref'] for i in items],
                'page_numbers':pages,'chunk_index':index}
            source_version='sha256-'+entry['sha256'][:12]
            identity={'source_id':entry['source_id'],'source_version':source_version,'locator':locator,'text':text}
            row={'schema_version':'knowledge-chunk/1.0','chunk_id':'kco_'+sha(json.dumps(identity,ensure_ascii=False,sort_keys=True).encode())[:32],
                'source_id':entry['source_id'],'source_version':source_version,'source_content_sha256':entry['sha256'],
                'source_url':entry['url'],'source_title':entry['title'],'institution':entry['institution'],
                'edition':entry['edition'],'jurisdiction':entry['jurisdiction'],'knowledge_scope':'external-official-background',
                'locator':locator,'section_path':headings,'page_start':min(pages) if pages else None,'page_end':max(pages) if pages else None,
                'text':text,'text_sha256':sha(text.encode()),'char_count':len(text),'token_count':count,
                'tokenizer_id':'bge-m3-pinned-tokenizer/640','ordinal':index,'language':'en',
                'chunk_type':'table' if any(i['label']=='table' for i in items) else 'text',
                'access_scope':'public-official-reference','exact_terms':sorted(set(re.findall(r'I062/\d{3}|FAA-H-\d+-\d+[A-Z]?|WMO-No\.?\s*8|ERA5|RNAV|RNP|QNH|QNE|WGS[- ]?84',text,re.I))),
                'created_at':datetime.now(timezone.utc).isoformat()}
            rows.append(row);added+=1
        stats.append({'source_id':entry['source_id'],'component':entry['filename'],'extraction':entry['extraction_path'],
            'pages':len(doc.pages),'text_items':len(doc.texts),'tables':len(doc.tables),'chunks':added,
            'document_json_sha256':doc_digest})
        print(json.dumps({'chunked':entry['filename'],'part':entry.get('page_range'),'added':added}),flush=True)
    audit={'components':stats,'restored_native_formula_text':restored,'unresolved_formulas':unresolved,
        'quarantined_chunks':skipped,'chunks_by_source':dict(Counter(r['source_id'] for r in rows)),
        'new_chunks':len(rows),'maximum_embedding_tokens':max(r['token_count'] for r in rows),
        'figures':'Original diagrams archived; no visual/VLM interpretation claimed',
        'formula_method':'Exact Docling native orig text only; no LLM transcription; spatial notation may require original-page inspection'}
    return rows,audit

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--pilot',action='store_true');parser.add_argument('--metadata-only',action='store_true');args=parser.parse_args()
    if args.metadata_only:
        if (TARGET/'ready.json').exists():raise ValueError('Finalize metadata before ready marker')
        save(TARGET/'rag_generation.v1.json',generation_metadata(read(TARGET/'rag_generation.v1.json'),read(TARGET/'ingest-audit.json')))
        print('Generation metadata finalized with 16 sources and explicit mixed chunk policies.');return
    entries=components(args.pilot)
    if not args.pilot:
        manifest=read(BATCH/'extraction-manifest.json');assert not manifest['errors']
        if (TARGET/'ready.json').exists():raise ValueError('Immutable generation already finalized')
    rows,audit=make_rows(entries)
    if args.pilot:
        save(BATCH/'qa/chunking-pilot.json',audit)
        (BATCH/'qa/pilot-chunks.jsonl').write_text(''.join(json.dumps(r,ensure_ascii=False)+'\n' for r in rows),encoding='utf8')
        print(json.dumps({k:v for k,v in audit.items() if k in ('new_chunks','chunks_by_source','maximum_embedding_tokens')}));return
    raw=(OLD/'rag_chunks.v1.jsonl').read_bytes()
    assert sha(raw)=='208c2322f951ac53028dcb999b75222d4ed4cac945d4122c76dd9544f9d38c96'
    parent=[json.loads(line) for line in raw.decode().splitlines()]
    all_rows=parent+rows
    assert len({r['chunk_id'] for r in all_rows})==len(all_rows)
    assert len(audit['chunks_by_source'])==8
    TARGET.mkdir(parents=True,exist_ok=True)
    path=TARGET/'rag_chunks.v1.jsonl'
    path.write_bytes(raw+(b'' if raw.endswith(b'\n') else b'\n')+''.join(json.dumps(r,ensure_ascii=False,sort_keys=True)+'\n' for r in rows).encode())
    assert path.read_bytes().startswith(raw)
    save(TARGET/'ingest-audit.json',audit)
    save(TARGET/'source-registry.json',manifest['documents'])
    generation=read(OLD/'rag_generation.v1.json')
    generation.update(generation_id=TARGET.name,created_at=datetime.now(timezone.utc).isoformat(),
        parent_generation_id=OLD.name,parent_corpus_sha256=sha(raw),corpus_sha256=sha(path.read_bytes()),
        chunk_count=len(all_rows),parser_id='docling-standard-and-html',parser_version='2.151.1',
        new_source_families=manifest['families'],original_149_records_unchanged=True)
    generation['sources']+= [{'source_id':family,'title':ALIASES[family],'scope':'external-official-background',
        'components':[r for r in manifest['documents'] if r['source_id']==family]} for family in manifest['families']]
    generation.pop('corpus_manifest_sha256',None)
    save(TARGET/'rag_generation.v1.json',generation_metadata(generation,audit))
    print(json.dumps({'status':'corpus_built','chunks':len(all_rows),'new_chunks':len(rows),'corpus_sha256':sha(path.read_bytes())}),flush=True)

if __name__=='__main__':main()
