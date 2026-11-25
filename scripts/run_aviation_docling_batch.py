"""Convert all approved source components with cached local Docling models."""
from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor,as_completed
import argparse,hashlib,json,os
from pathlib import Path
import subprocess,sys,time

ROOT=Path(__file__).resolve().parents[1]
BATCH=ROOT/'var/rag/sources/official-aviation-20261123'
PYTHON='/home/h3c/anaconda3/envs/docling/bin/python'
os.environ.update(HF_HUB_OFFLINE='1',TRANSFORMERS_OFFLINE='1',TOKENIZERS_PARALLELISM='false',OMP_NUM_THREADS='8')

def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()

def pdf_job(row,start,end):
    source=BATCH/row['relative_path']
    folder=BATCH/'extracted'/row['source_id']/f'{source.stem}-p{start:04d}-{end:04d}'
    record={**row,'page_range':[start,end],'extraction_path':str(folder.relative_to(BATCH)),'input_sha256':row['sha256']}
    status=folder/'conversion.json'
    if status.exists():
        value=json.loads(status.read_text())
        assert value['source_sha256']==row['sha256'] and value['status']=='success'
        return record
    folder.mkdir(parents=True,exist_ok=True)
    with (folder/'process.log').open('w') as log:
        p=subprocess.run([PYTHON,'-B',str(ROOT/'scripts/extract_aviation_docling.py'),str(source),str(folder),'--start',str(start),'--end',str(end)],cwd=ROOT,stdout=log,stderr=subprocess.STDOUT,timeout=3600)
    if p.returncode:raise RuntimeError(f'PDF extraction failed: {source.name} {start}-{end}; see {folder}/process.log')
    return record

def html_jobs(rows):
    from bs4 import BeautifulSoup
    from docling.document_converter import DocumentConverter
    from docling.datamodel.base_models import InputFormat
    converter=DocumentConverter(allowed_formats=[InputFormat.HTML])
    records=[]
    for row in rows:
        source=BATCH/row['relative_path']
        folder=BATCH/'extracted'/row['source_id']/source.stem
        record={**row,'extraction_path':str(folder.relative_to(BATCH)),'input_sha256':row['sha256']}
        if (folder/'conversion.json').exists():
            value=json.loads((folder/'conversion.json').read_text())
            assert value['source_sha256']==row['sha256'] and value['status']=='success'
            records.append(record);continue
        soup=BeautifulSoup(source.read_bytes(),'html.parser')
        if row['source_id']=='ecmwf-era5-documentation':content=soup.select_one('#main-content')
        elif row['source_id']=='faa-pilot-controller-glossary':content=soup.select_one('article.pcg-content')
        else:content=soup.find('main')
        if not content or len(content.get_text(' ',strip=True))<20:raise ValueError('Missing HTML content: '+source.name)
        for element in content.select('script,style,nav,header,footer,.sr-only'):
            element.decompose()
        # Official glossary marks terms with dfn. Promote those explicit labels
        # to headings without rewriting the definition text.
        for term in content.select('dfn.term-name'):
            heading=soup.new_tag('h2');heading.string=term.get_text(' ',strip=True)
            term.parent.insert_before(heading)
        for image in content.find_all('img'):
            marker=soup.new_tag('p');marker.string='[Figure: '+image.get('alt','image in archived original')+'; consult archived original for graphical content]'
            image.replace_with(marker)
        folder.mkdir(parents=True,exist_ok=True)
        cleaned=folder/'input.cleaned.html'
        cleaned.write_text('<!doctype html><html lang="en"><head><meta charset="utf-8"></head><body><h1>'+row['title'].replace('&','&amp;').replace('<','&lt;')+'</h1>'+str(content)+'</body></html>',encoding='utf8')
        started=time.time();result=converter.convert(cleaned,raises_on_error=False)
        if result.status.value!='success':raise ValueError('HTML conversion failed: '+source.name)
        result.document.save_as_json(folder/'document.json');result.document.save_as_markdown(folder/'document.md')
        stats={'status':'success','source_sha256':row['sha256'],'cleaned_input_sha256':sha(cleaned),'pages':0,
            'text_items':len(result.document.texts),'tables':len(result.document.tables),'seconds':time.time()-started,
            'pipeline':'Docling HTML backend; navigation removed; original archived; figures referenced not interpreted'}
        (folder/'conversion.json').write_text(json.dumps(stats,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
        records.append(record)
        if len(records)%10==0:print(json.dumps({'html_progress':len(records),'total':len(rows)}),flush=True)
    return records

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--html-only',action='store_true');parser.add_argument('--assemble-only',action='store_true');args=parser.parse_args()
    import pypdfium2
    rows=[json.loads(p.read_text()) for p in sorted((BATCH/'originals').glob('*.metadata.json'))]
    for row in rows:assert sha(BATCH/row['relative_path'])==row['sha256']
    families=sorted({r['source_id'] for r in rows});assert len(families)==8,families
    pdfs=[r for r in rows if r['filename'].endswith('.pdf') and r['source_id'] not in ('faa-aim','faa-pilot-controller-glossary')]
    html=[r for r in rows if r['filename'].endswith('.html')]
    if args.html_only:
        records=html_jobs(html)
        (BATCH/'html-extractions.json').write_text(json.dumps(records,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
        print(json.dumps({'html_converted':len(records)}),flush=True);return
    tasks=[];records=[]
    for row in pdfs:
        if row['source_id']=='eurocontrol-asterix-cat062':
            folder=BATCH/'extracted'/row['source_id']
            status=json.loads((folder/'conversion.json').read_text())
            assert status['status']=='success' and status['source_sha256']==row['sha256']
            records.append({**row,'page_range':[1,status['pages']],'extraction_path':str(folder.relative_to(BATCH)),'input_sha256':row['sha256']})
            continue
        count=len(pypdfium2.PdfDocument(BATCH/row['relative_path']))
        for start in range(1,count+1,100):tasks.append((row,max(1,start-2),min(count,start+99)))
    print(json.dumps({'pdf_tasks':len(tasks),'html_components':len(html),'families':families}),flush=True)
    errors=[]
    if args.assemble_only:
        records+=json.loads((BATCH/'html-extractions.json').read_text())
        for row,start,end in tasks:
            source=BATCH/row['relative_path'];folder=BATCH/'extracted'/row['source_id']/f'{source.stem}-p{start:04d}-{end:04d}'
            if not (folder/'conversion.json').exists():raise RuntimeError('Extraction not complete: '+str(folder))
            records.append(pdf_job(row,start,end))
    else:
        # Isolate HTML conversion before starting PDF worker threads. Some
        # native libraries do not safely initialize after concurrent PDF use.
        completed=subprocess.run([PYTHON,'-B',str(Path(__file__)),'--html-only'],cwd=ROOT,timeout=1800)
        if completed.returncode:raise RuntimeError('HTML conversion subprocess failed')
        records+=json.loads((BATCH/'html-extractions.json').read_text())
        with ThreadPoolExecutor(max_workers=4) as pool:
            jobs={pool.submit(pdf_job,*task):task for task in tasks}
            for job in as_completed(jobs):
                task=jobs[job]
                try:
                    records.append(job.result());print(json.dumps({'pdf_done':task[0]['filename'],'pages':[task[1],task[2]]}),flush=True)
                except Exception as exc:
                    errors.append({'file':task[0]['filename'],'pages':[task[1],task[2]],'error':str(exc)})
    manifest={'schema_version':1,'families':families,'documents':rows,'extractions':records,'errors':errors,
        'docling_project':'/home/h3c/project/python/docling','conda_environment':PYTHON,
        'no_remote_model_calls':True,'pdf_overlap_pages':2,'pdf_models':'existing local cached layout + table models',
        'original_pdf_archived_html_preferred':['faa-aim','faa-pilot-controller-glossary']}
    (BATCH/'extraction-manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
    print(json.dumps({'status':'completed' if not errors else 'failed','extractions':len(records),'errors':errors}),flush=True)
    if errors:raise SystemExit(1)

if __name__=='__main__':main()
