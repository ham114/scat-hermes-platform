"""Download the eight approved official aviation source families, with provenance."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from urllib.parse import urlparse

import requests

ROOT = Path(__file__).resolve().parents[1]
BATCH = ROOT / 'var/rag/sources/official-aviation-20261123'
SOURCES = [
    dict(source_id='wmo-no8-volume1', title='Guide to Instruments and Methods of Observation, Volume I: Measurement of Meteorological Variables', institution='WMO', edition='WMO-No.8, Volume I, 2024', jurisdiction='meteorological observations; not device-specific UAV mappings', filename='wmo-8-volume1-2024-en.pdf', url='https://library.wmo.int/viewer/68695/download?file=WMO-8-vI-2024_en.pdf&type=pdf&navigator=1', landing_url='https://library.wmo.int/records/item/68695-guide-to-instruments-and-methods-of-observation'),
    dict(source_id='eurocontrol-asterix-cat062', title='ASTERIX Part 9 CAT062 System Track Data', institution='EUROCONTROL', edition='1.21, 2025-06-05', jurisdiction='protocol-standard', filename='asterix-cat062-ed1-21.pdf', url='https://www.eurocontrol.int/sites/default/files/2025-06/asterix-cat062-system-track-data-p9-ed1-21.pdf', landing_url='https://www.eurocontrol.int/publication/cat062-eurocontrol-specification-surveillance-data-exchange-asterix-part-9-category-062'),
    dict(source_id='faa-phak', title="Pilot's Handbook of Aeronautical Knowledge", institution='FAA', edition='FAA-H-8083-25C', jurisdiction='US; general flight background', filename='faa-h-8083-25c.pdf', url='https://www.faa.gov/regulations_policies/handbooks_manuals/aviation/faa-h-8083-25c.pdf', landing_url='https://www.faa.gov/regulations_policies/handbooks_manuals/aviation/phak'),
    dict(source_id='faa-pilot-controller-glossary', title='Pilot/Controller Glossary', institution='FAA', edition='Basic with Changes 1,2,3; effective 2026-07-09', jurisdiction='US; ICAO definitions explicitly tagged', filename='faa-pcg-20260709.pdf', url='https://www.faa.gov/air_traffic/publications/media/PCG_Bsc_w_Chg_1_2_and_3_dtd_7-9-26.pdf', landing_url='https://www.faa.gov/air_traffic/publications/atpubs/pcg_html/index.html'),
    dict(source_id='faa-instrument-flying-handbook', title='Instrument Flying Handbook', institution='FAA', edition='FAA-H-8083-15B; base 2012, supplements tracked separately', jurisdiction='US; general instrument-flight background', filename='faa-h-8083-15b.pdf', url='https://www.faa.gov/sites/faa.gov/files/regulations_policies/handbooks_manuals/aviation/FAA-H-8083-15B.pdf', landing_url='https://www.faa.gov/regulations_policies/handbooks_manuals/aviation'),
    dict(source_id='faa-aviation-weather-handbook', title='Aviation Weather Handbook', institution='FAA', edition='FAA-H-8083-28B; 2026', jurisdiction='US; general aviation weather background', filename='faa-h-8083-28b.pdf', url='https://www.faa.gov/sites/faa.gov/files/FAA-H-8083-28B.pdf', landing_url='https://www.faa.gov/regulationspolicies/handbooksmanuals/aviation/faa-h-8083-28b-aviation-weather-handbook'),
    dict(source_id='faa-aim', title='Aeronautical Information Manual', institution='FAA', edition='Basic with Changes 1,2,3; effective 2026-07-09', jurisdiction='US', filename='faa-aim-20260709.pdf', url='https://www.faa.gov/air_traffic/publications/media/aim_basic_w_chg1_chg2_and_chg3_dtd_7_9_26_notice_to_airmen.pdf', landing_url='https://www.faa.gov/air_traffic/publications/atpubs/aim_html/index.html'),
    dict(source_id='ecmwf-era5-documentation', title='ERA5 Data Documentation', institution='ECMWF / Copernicus', edition='official current page snapshot at retrieval time', jurisdiction='ERA5 product only; not SCAT GRIB provenance', filename='era5-documentation.html', url='https://confluence.ecmwf.int/spaces/CKB/pages/76414402/ERA5%2Bdata%2Bdocumentation', landing_url='https://confluence.ecmwf.int/spaces/CKB/pages/76414402/ERA5%2Bdata%2Bdocumentation'),
]

def fetch(row: dict) -> dict:
    target = BATCH / 'originals' / row['filename']
    metadata = target.with_suffix(target.suffix + '.metadata.json')
    if metadata.exists() and target.exists():
        value = json.loads(metadata.read_text(encoding='utf8'))
        assert hashlib.sha256(target.read_bytes()).hexdigest() == value['sha256']
        return value
    session = requests.Session()
    response = session.get(row['url'], stream=True, timeout=(20, 30))
    response.raise_for_status()
    host = urlparse(response.url).hostname or ''
    if not any(host == suffix or host.endswith('.' + suffix) for suffix in ('faa.gov', 'eurocontrol.int', 'ecmwf.int', 'wmo.int')):
        raise ValueError('Unexpected non-official redirect: ' + host)
    temporary = target.with_suffix(target.suffix + '.part')
    digest = hashlib.sha256()
    size = 0
    with temporary.open('wb') as stream:
        for block in response.iter_content(64 * 1024):
            size += len(block)
            if size > 300 * 1024 * 1024:
                raise ValueError('Document exceeded 300 MiB bound')
            stream.write(block)
            digest.update(block)
    with temporary.open('rb') as stream:
        prefix = stream.read(100)
    if target.suffix == '.pdf' and not prefix.startswith(b'%PDF-'):
        raise ValueError('Expected PDF, got another content type: ' + row['source_id'])
    temporary.replace(target)
    result = {**row, 'effective_url':response.url, 'content_type':response.headers.get('Content-Type'), 'retrieved_at':datetime.now(timezone.utc).isoformat(), 'sha256':digest.hexdigest(), 'bytes':size, 'relative_path':target.relative_to(BATCH).as_posix()}
    metadata.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf8')
    return result

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--registry', type=Path)
    parser.add_argument('--adopt-wmo', type=Path)
    args=parser.parse_args()
    (BATCH/'originals').mkdir(parents=True, exist_ok=True)
    if args.adopt_wmo:
        source=args.adopt_wmo.resolve()
        if source.parent != (BATCH/'originals').resolve():
            raise ValueError('WMO source must be in this batch originals directory')
        with source.open('rb') as stream:
            if not stream.read(8).startswith(b'%PDF-'):raise ValueError('Not a PDF')
        row=next(r for r in SOURCES if r['source_id']=='wmo-no8-volume1')
        result={**row,'filename':source.name,'relative_path':source.relative_to(BATCH).as_posix(),
            'retrieved_at':datetime.now(timezone.utc).isoformat(),'sha256':hashlib.sha256(source.read_bytes()).hexdigest(),
            'bytes':source.stat().st_size,'retrieval_mode':'user downloaded official document after interactive verification',
            'content_type':'application/pdf (file signature verified)'}
        source.with_suffix(source.suffix+'.metadata.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
        print(json.dumps(result,ensure_ascii=False))
        return
    # The WMO download has an interactive verification step. Keep it explicit.
    rows=json.loads(args.registry.read_text(encoding='utf8')) if args.registry else [r for r in SOURCES if r['source_id']!='wmo-no8-volume1']
    done=[];errors=[]
    with ThreadPoolExecutor(max_workers=3) as pool:
        tasks={pool.submit(fetch, row):row for row in rows}
        for task in as_completed(tasks):
            row=tasks[task]
            try:
                value=task.result();done.append(value)
                print(json.dumps({'source_id':row['source_id'],'status':'downloaded','bytes':value['bytes'],'sha256':value['sha256']},ensure_ascii=False),flush=True)
            except Exception as exc:
                errors.append({'source_id':row['source_id'],'error_type':type(exc).__name__,'error':str(exc)})
                print(json.dumps(errors[-1]),flush=True)
    (BATCH/'download-status.json').write_text(json.dumps({'downloads':done,'errors':errors},ensure_ascii=False,indent=2)+'\n',encoding='utf8')
    if errors:raise SystemExit(1)

if __name__=='__main__':main()
