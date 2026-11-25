"""Inspect installed chunking interfaces and render source pages for extraction QA."""
import json,os
from pathlib import Path
os.environ.update(HF_HUB_OFFLINE='1',TRANSFORMERS_OFFLINE='1',TOKENIZERS_PARALLELISM='false')
from docling_core.types.doc import DoclingDocument
from docling_core.transforms.chunker.hybrid_chunker import HybridChunker
from docling_core.transforms.chunker.tokenizer.huggingface import HuggingFaceTokenizer
from transformers import AutoTokenizer
import pypdfium2

ROOT=Path(__file__).resolve().parents[1]
B=ROOT/'var/rag/sources/official-aviation-20261123'
doc=DoclingDocument.load_from_json(B/'extracted/eurocontrol-asterix-cat062/document.json')
tokenizer=AutoTokenizer.from_pretrained('/mnt/h3c/scat-rag/models/embedding/bge-m3-5617a9f61b028005a4858fdac845db406aefb181',local_files_only=True)
chunker=HybridChunker(tokenizer=HuggingFaceTokenizer(tokenizer=tokenizer,max_tokens=640),merge_peers=True,repeat_table_header=True)
chunks=list(chunker.chunk(doc))
print('CHUNKS',len(chunks))
for chunk in chunks[:2]:print(json.dumps({'text':chunker.contextualize(chunk=chunk),'meta':chunk.meta.model_dump(mode='json')},ensure_ascii=False)[:1600])
formulas=[x for x in doc.texts if str(x.label.value)=='formula']
print('FORMULAS',len(formulas))
for item in formulas[:4]:print(item.model_dump_json()[:1100])
pdf=pypdfium2.PdfDocument(B/'originals/asterix-cat062-ed1-21.pdf')
(B/'qa').mkdir(exist_ok=True)
for number in (51,52):
    page=pdf[number-1]
    page.render(scale=1.6).to_pil().save(B/'qa'/f'cat062-page-{number}.png')
    print('PAGE',number,page.get_textpage().get_text_bounded()[:2600])
