"""Cross-check selected PDF phrases with an independent native parser and RAG text."""
import json,re
from pathlib import Path
import pypdfium2

ROOT=Path(__file__).resolve().parents[1]
B=ROOT/'var/rag/sources/official-aviation-20261123'
rows=[json.loads(line) for line in (ROOT/'var/rag/corpora/kg_20261123_aviation_v5/rag_chunks.v1.jsonl').read_text().splitlines()]
checks=[
 ('eurocontrol-asterix-cat062','asterix-cat062-ed1-21.pdf',51,'1/4 FL'),
 ('eurocontrol-asterix-cat062','asterix-cat062-ed1-21.pdf',52,'0.25 m/s'),
 ('faa-phak','faa-h-8083-25c.pdf',79,'relative wind'),
 ('faa-instrument-flying-handbook','faa-h-8083-15b.pdf',97,'static pressure'),
 ('faa-aviation-weather-handbook','faa-h-8083-28b.pdf',258,'microburst winds'),
 ('wmo-no8-volume1','WMO-8-vI-2024_en.pdf',36,'true north'),
 ('wmo-no8-volume1','WMO-8-vI-2024_en.pdf',36,'hectopascals'),
]
norm=lambda x:re.sub(r'\s+',' ',x).casefold()
results=[]
for family,filename,page,needle in checks:
    pdf=pypdfium2.PdfDocument(B/'originals'/filename)
    surface=pdf[page-1];tp=surface.get_textpage();native=tp.get_text_bounded();tp.close();surface.close();pdf.close()
    hits=[r for r in rows if r['source_id']==family and page in r['locator'].get('page_numbers',[]) and norm(needle) in norm(r['text'])]
    if not (norm(needle) in norm(native) and hits):
        print(json.dumps({'source_id':family,'page':page,'phrase':needle,'native_found':norm(needle) in norm(native),
            'same_page_chunks':[{'id':r['chunk_id'],'text':r['text']} for r in rows if r['source_id']==family and page in r['locator'].get('page_numbers',[])],
            'other_page_hits':[{'id':r['chunk_id'],'pages':r['locator'].get('page_numbers')} for r in rows if r['source_id']==family and norm(needle) in norm(r['text'])][:8]},ensure_ascii=False))
        raise AssertionError((family,page,needle))
    results.append({'source_id':family,'page':page,'phrase':needle,'independent_pdf_text_found':True,'rag_chunk_ids':[r['chunk_id'] for r in hits]})
for family,needle in [('faa-pilot-controller-glossary','FLIGHT LEVEL'),('faa-aim','performance monitoring and alerting'),('ecmwf-era5-documentation','geopotential')]:
    hits=[r for r in rows if r['source_id']==family and norm(needle) in norm(r['text'])]
    assert hits,(family,needle)
    results.append({'source_id':family,'phrase':needle,'rag_chunk_ids':[r['chunk_id'] for r in hits[:5]],'method':'official HTML text presence; not a full semantic accuracy check'})
result={'status':'passed','checks':results,'visual_review':'6 rendered source pages inspected by assistant','external_model_calls':0}
(B/'qa/extraction-samples.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
print(json.dumps({'status':'passed','checks':len(results)}))
