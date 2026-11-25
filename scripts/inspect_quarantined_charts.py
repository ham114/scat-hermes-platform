"""Render frozen problem chart regions and extract native text, offline."""
import hashlib
import json
from pathlib import Path
import pypdfium2

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'output/rag-chart-rewrite-20261124'
OUT.mkdir(parents=True, exist_ok=True)
rows = [json.loads(x) for x in (ROOT/'var/rag/corpora/kg_20261124_aviation_v6_p0/rag_chunks.v1.jsonl').read_text().splitlines()]
items = json.loads((ROOT/'output/rag-p0-repair-20261124/source-quarantine-register.json').read_text())
result = []
for index, item in enumerate(items):
    artifact = json.loads((ROOT/item['artifact']).read_text())
    owner = next(r for r in rows if r['locator'].get('structured_table_json') == item['artifact'])
    source = ROOT/'var/rag/sources/official-aviation-20261123/originals'/owner['locator']['component']
    assert hashlib.sha256(source.read_bytes()).hexdigest() == owner['source_content_sha256']
    pdf = pypdfium2.PdfDocument(source)
    prov = artifact['original_table']['prov'][0]
    page = pdf[prov['page_no']-1]
    bbox = prov['bbox']; width, height = page.get_size()
    left, right = max(0,bbox['l']-24), min(width,bbox['r']+24)
    if bbox['coord_origin'] == 'BOTTOMLEFT':
        bottom, top = max(0,bbox['b']-32),min(height,bbox['t']+40)
    else:
        bottom,top=max(0,height-bbox['b']-32),min(height,height-bbox['t']+40)
    textpage=page.get_textpage()
    native=textpage.get_text_bounded(left,bottom,right,top)
    exact=textpage.get_text_bounded(bbox['l'],bbox['b'],bbox['r'],bbox['t'])
    full=textpage.get_text_bounded()
    bitmap=page.render(scale=2).to_pil()
    bitmap.crop((int(left*2),int((height-top)*2),int(right*2),int((height-bottom)*2))).save(OUT/f'chart-{index:02d}.png')
    result.append({**item,'index':index,'source_pdf':str(source.relative_to(ROOT)),'source_sha256':owner['source_content_sha256'],
                   'section_path':owner['section_path'],'image':f'chart-{index:02d}.png','native_region_text':native,'native_exact_text':exact,'native_page_text':full,
                   'original_cells':[c['text'] for c in artifact['original_table']['data']['table_cells']]})
    textpage.close(); page.close(); pdf.close()
(OUT/'source-inspection.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
print(json.dumps({'rendered_charts':len(result),'external_model_calls':0}))
