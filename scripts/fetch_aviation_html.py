"""Fetch linked official HTML editions and supplements for the approved families."""
from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor,as_completed
import json
from pathlib import Path
import re
from urllib.parse import urljoin
from bs4 import BeautifulSoup
from fetch_aviation_official import BATCH,SOURCES,fetch

def main():
    rows=[]
    for key,family,base,pattern in [
        ('aim','faa-aim','https://www.faa.gov/air_traffic/publications/atpubs/aim_html/',r'(chap\d+_section_\d+|appendix_\d+)\.html$'),
        ('pcg','faa-pilot-controller-glossary','https://www.faa.gov/air_traffic/publications/atpubs/pcg_html/',r'glossary-[a-z]\.html$')]:
        soup=BeautifulSoup((BATCH/'discovery'/f'{key}.html').read_bytes(),'html.parser')
        parent=next(r for r in SOURCES if r['source_id']==family)
        found={}
        for link in soup.find_all('a',href=True):
            url=urljoin(base,link['href'])
            if re.search(pattern,url):found[url]=link.get_text(' ',strip=True)
        for url,title in sorted(found.items()):
            name=url.rsplit('/',1)[1]
            rows.append({**parent,'title':parent['title']+' — '+title,'filename':key+'-'+name,'url':url,
                'component_id':name,'representation':'official HTML; same effective edition as archived PDF'})
    soup=BeautifulSoup((BATCH/'discovery/handbooks.html').read_bytes(),'html.parser')
    for link in soup.find_all('a',href=True):
        href=link['href']
        if not any(name in href for name in ('ifh_errata.pdf','ifh_addendum.pdf','ifh_addendum_b.pdf','PHAK_Addendum_')):continue
        family='faa-phak' if 'PHAK_' in href else 'faa-instrument-flying-handbook'
        parent=next(r for r in SOURCES if r['source_id']==family)
        name=href.rsplit('/',1)[1]
        rows.append({**parent,'title':parent['title']+' — '+link.get_text(' ',strip=True),
            'filename':name,'url':urljoin('https://www.faa.gov',href),'component_id':name,
            'edition':'official errata/addendum to archived handbook; see original publication date'})
    if not 65<=len(rows)<=100:raise ValueError(f'Unexpected component count: {len(rows)}')
    (BATCH/'component-registry.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
    done=[];errors=[]
    with ThreadPoolExecutor(max_workers=4) as pool:
        jobs={pool.submit(fetch,row):row for row in rows}
        for job in as_completed(jobs):
            row=jobs[job]
            try:
                done.append(job.result())
                print(json.dumps({'status':'downloaded','source_id':row['source_id'],'component':row['filename']},ensure_ascii=False),flush=True)
            except Exception as exc:errors.append({'filename':row['filename'],'type':type(exc).__name__,'error':str(exc)})
    (BATCH/'html-download-status.json').write_text(json.dumps({'downloads':done,'errors':errors},ensure_ascii=False,indent=2)+'\n',encoding='utf8')
    print(json.dumps({'components':len(rows),'downloaded':len(done),'errors':errors}),flush=True)
    if errors:raise SystemExit(1)

if __name__=='__main__':main()
