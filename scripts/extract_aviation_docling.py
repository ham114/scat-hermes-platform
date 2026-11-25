"""Offline Docling extraction of approved public aviation documents."""
from __future__ import annotations
import argparse
import hashlib
import importlib.metadata
import json
import math
import os
from pathlib import Path
import time

os.environ.update(HF_HUB_OFFLINE='1',TRANSFORMERS_OFFLINE='1',TOKENIZERS_PARALLELISM='false',OMP_NUM_THREADS='8')

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('source',type=Path)
    parser.add_argument('output',type=Path)
    parser.add_argument('--start',type=int,default=1)
    parser.add_argument('--end',type=int,default=2147483647)
    parser.add_argument('--ocr',action='store_true')
    args=parser.parse_args()
    from docling.document_converter import DocumentConverter,PdfFormatOption
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import PdfPipelineOptions,HeadingHierarchyOptions
    from docling.datamodel.accelerator_options import AcceleratorOptions,AcceleratorDevice
    options=PdfPipelineOptions(do_ocr=args.ocr,do_table_structure=True,enable_remote_services=False,
        do_code_enrichment=False,do_formula_enrichment=False,generate_page_images=False,
        heading_hierarchy_options=HeadingHierarchyOptions(enabled=True),
        accelerator_options=AcceleratorOptions(num_threads=8,device=AcceleratorDevice.CPU))
    converter=DocumentConverter(format_options={InputFormat.PDF:PdfFormatOption(pipeline_options=options)})
    args.output.mkdir(parents=True,exist_ok=True)
    started=time.time()
    result=converter.convert(args.source,page_range=(args.start,args.end),raises_on_error=False)
    document=result.document
    if result.status.value!='success':
        raise RuntimeError(f'Docling conversion failed: {result.status}; {result.errors}')
    document.save_as_json(args.output/'document.json')
    document.save_as_markdown(args.output/'document.md')
    stats={'source':str(args.source),'source_sha256':hashlib.sha256(args.source.read_bytes()).hexdigest(),
        'status':result.status.value,'pages':len(document.pages),'text_items':len(document.texts),'tables':len(document.tables),
        'seconds':time.time()-started,'docling_version':importlib.metadata.version('docling'),
        'pipeline':'standard; CPU; local cached models; remote services disabled','ocr':args.ocr,
        'requested_page_range':[args.start,args.end],
        'confidence':result.confidence.model_dump(mode='json')}
    def finite(value):
        if isinstance(value,float) and not math.isfinite(value):return None
        if isinstance(value,dict):return {k:finite(v) for k,v in value.items()}
        if isinstance(value,list):return [finite(v) for v in value]
        return value
    stats=finite(stats)
    (args.output/'conversion.json').write_text(json.dumps(stats,ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf8')
    print(json.dumps(stats,ensure_ascii=False),flush=True)

if __name__=='__main__':main()
