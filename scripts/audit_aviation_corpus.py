"""Independent artifact, page-coverage and citation audit; no model requests."""
from __future__ import annotations
from collections import Counter,defaultdict
import hashlib,json,re
from pathlib import Path
import pypdfium2

ROOT=Path(__file__).resolve().parents[1]
BATCH=ROOT/'var/rag/sources/official-aviation-20261123'
TARGET=ROOT/'var/rag/corpora/kg_20261123_aviation_v5'
OLD=ROOT/'var/rag/corpora/kg_20261026_research_v4'
def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()
def read(path):return json.loads(path.read_text())

def main():
    manifest=read(BATCH/'extraction-manifest.json');assert not manifest['errors']
    originals={r['filename']:r for r in manifest['documents']}
    counts={};source_checks=[]
    for filename,row in originals.items():
        p=BATCH/row['relative_path'];assert sha(p)==row['sha256']
        if p.suffix=='.pdf':
            pdf=pypdfium2.PdfDocument(p);counts[filename]=len(pdf);pdf.close()
        source_checks.append({'source_id':row['source_id'],'filename':filename,'bytes':p.stat().st_size,'sha256':row['sha256'],'pdf_pages':counts.get(filename)})
    coverage=defaultdict(set);artifact_hashes={}
    for entry in manifest['extractions']:
        folder=BATCH/entry['extraction_path'];status=read(folder/'conversion.json')
        assert status['status']=='success' and status['source_sha256']==entry['sha256']
        doc=read(folder/'document.json');normalized=read(folder/'document.rag.json')
        assert len(doc['texts'])==len(normalized['texts'])
        for old,new in zip(doc['texts'],normalized['texts'],strict=True):
            if old['text']!=new['text']:
                assert old['label']=='formula' and not old['text'].strip() and new['text']==old['orig']
        artifact_hashes[str((folder/'document.rag.json').relative_to(ROOT))]=sha(folder/'document.rag.json')
        if entry.get('page_range'):
            start,end=entry['page_range'];pages=set(map(int,doc['pages']))
            assert pages==set(range(start,end+1)),(entry['filename'],start,end,len(pages))
            coverage[entry['filename']]|=pages
    for filename,pages in coverage.items():assert pages==set(range(1,counts[filename]+1)),filename
    raw=(TARGET/'rag_chunks.v1.jsonl').read_bytes();parent=(OLD/'rag_chunks.v1.jsonl').read_bytes()
    assert raw.startswith(parent)
    rows=[json.loads(line) for line in raw.decode().splitlines()]
    assert len(rows)==len({r['chunk_id'] for r in rows})
    families=Counter();checked=0
    for row in rows[149:]:
        assert hashlib.sha256(row['text'].encode()).hexdigest()==row['text_sha256']
        assert row['source_id'] in manifest['families'] and row['source_content_sha256'] in {r['sha256'] for r in manifest['documents']}
        locator=row['locator'];assert artifact_hashes[locator['document_json']]==locator['document_json_sha256']
        if locator['page_numbers']:assert set(locator['page_numbers'])<=coverage[locator['component']]
        assert row['token_count']<=1800 and row['text'].strip()
        families[row['source_id']]+=1;checked+=1
    assert len(families)==8
    for needle in ('1/4 FL','bits-32/17 Vx','0.25 m/s'):
        assert any(needle in r['text'] for r in rows[149:] if r['source_id']=='eurocontrol-asterix-cat062'),needle
    ingest=read(TARGET/'ingest-audit.json')
    result={'status':'passed','original_files_checked':len(originals),'source_families':8,
        'pdf_original_pages':sum(counts.values()),'pdf_unique_extracted_pages':sum(map(len,coverage.values())),
        'html_components':sum(r['filename'].endswith('.html') for r in originals.values()),
        'pdf_page_coverage':{k:{'covered':len(v),'total':counts[k]} for k,v in coverage.items()},
        'docling_artifacts_checked':len(manifest['extractions']),'original_149_records_unchanged':True,
        'new_chunks_checked':checked,'total_chunks':len(rows),'chunks_by_source':dict(families),
        'restored_native_formulas':len(ingest['restored_native_formula_text']),
        'unresolved_formula_items':len(ingest['unresolved_formulas']),'quarantined_chunks':len(ingest['quarantined_chunks']),
        'source_files':source_checks,'corpus_sha256':sha(TARGET/'rag_chunks.v1.jsonl'),
        'external_model_calls':0,'figure_content_not_transcribed':True}
    (TARGET/'artifact-audit.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    # Pick representative source pages for visual review; ignore contents pages.
    selections=[('faa-h-8083-25c.pdf','angle of attack',40),('faa-h-8083-15b.pdf','pitot-static',30),('faa-h-8083-28b.pdf','microburst',70),('WMO-8-vI-2024_en.pdf','true north',35)]
    visual=[]
    for filename,needle,minimum in selections:
        pdf=pypdfium2.PdfDocument(BATCH/originals[filename]['relative_path'])
        for i in range(minimum,len(pdf)):
            page=pdf[i];textpage=page.get_textpage();text=textpage.get_text_bounded();textpage.close()
            if needle.casefold() in text.casefold() and len(text)>800:
                png=BATCH/'qa'/f'{Path(filename).stem}-page-{i+1}.png'
                page.render(scale=1.4).to_pil().save(png)
                visual.append({'filename':filename,'page':i+1,'needle':needle,'image':str(png.relative_to(BATCH))})
                page.close();break
            page.close()
        pdf.close()
    (BATCH/'qa/visual-samples.json').write_text(json.dumps(visual,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({k:v for k,v in result.items() if k!='source_files'},ensure_ascii=False),flush=True)

if __name__=='__main__':main()
