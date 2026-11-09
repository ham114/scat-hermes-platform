"""Create only additive stable CFPL paging indexes on the shared derived collection."""

import json
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "var/validation/20261108-release-readiness"
CODE = r"""
import asyncio,json,time
from scat.infrastructure.database import flight_database
from scat.materialization.tasks import DEFAULT_SOURCE_VERSION,PREDICTION_METRICS_ALGORITHM_VERSION
async def main():
    await flight_database.connect()
    c=flight_database.get_collection('scat_prediction_metrics');rows=[]
    try:
        before=await c.index_information()
        for metric in ['horizontal_error_m','vertical_error_m','along_track_error_m','cross_track_error_m']:
            name='cfpl_paging_'+metric+'_v1';keys=[('batch',1),('source_version',1),('algorithm_version',1),('is_valid',1),(metric,-1),('external_id',1),('snapshot_time',1),('horizon_s',1)]
            start=time.monotonic()
            if name in before:assert list(before[name]['key'])==keys
            else:await c.create_index(keys,name=name,maxTimeMS=120000)
            query={'batch':'scat20161015_20161021','source_version':DEFAULT_SOURCE_VERSION,'algorithm_version':PREDICTION_METRICS_ALGORITHM_VERSION,'is_valid':True,metric:{'$ne':None}}
            result=await c.find(query,{'_id':0}).sort(keys[4:]).limit(3).explain()
            statistics=result.get('executionStats',{})
            rows.append({'name':name,'existing':name in before,'seconds':round(time.monotonic()-start,4),'keys_examined':statistics.get('totalKeysExamined'),'documents_examined':statistics.get('totalDocsExamined'),'returned':statistics.get('nReturned')})
        print(json.dumps({'status':'indexes_present','additive_only':True,'raw_collections_written':0,'indexes':rows}))
    finally:await flight_database.close()
asyncio.run(main())
"""


def main():
    os.umask(0o077)
    assert not (OUT / "cfpl-paging-indexes.json").exists(), "already_verified"
    p = subprocess.run(
        [
            "docker",
            "exec",
            "-i",
            "scat-hermes-h7b-platform-1",
            "/opt/conda/envs/scat-agent/bin/python",
            "-B",
            "-c",
            CODE,
        ],
        input="",
        capture_output=True,
        text=True,
        check=False,
        timeout=540,
    )
    private = OUT / "private"
    private.mkdir(exist_ok=True)
    (private / "cfpl-paging-indexes.stderr").write_text(p.stderr)
    assert p.returncode == 0, "index_build_failed_inspect_state_before_retry"
    report = json.loads(p.stdout)
    (OUT / "cfpl-paging-indexes.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))


if __name__ == "__main__":
    main()
