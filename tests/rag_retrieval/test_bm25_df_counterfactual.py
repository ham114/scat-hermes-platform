import math,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2];sys.path[:0]=[str(ROOT/'src'),str(ROOT/'scripts')]
from rag_retrieval.baseline import BM25Index,Chunk
from evidence_bm25_counterfactual import correct_document_frequency

def test_repeated_term_historical_corruption_is_corrected():
 chunks=[Chunk(str(i),'source','v',(),'signal '*10) for i in range(3)]
 original=BM25Index(chunks);experiment=BM25Index(chunks)
 # Explicitly recreate the historical defect; the implementation is now fixed.
 original._idf['signal']=experiment._idf['signal']=math.log(1+(3-30+.5)/(30+.5))
 assert original._idf['signal']<0 and original.rank('signal',limit=3)==[]
 audit=correct_document_frequency(experiment)
 assert math.isclose(experiment._idf['signal'],math.log(1+.5/3.5))
 assert len(experiment.rank('signal',limit=3))==3 and audit['negative_idf_after']==0
 assert original._idf['signal']<0 and original.rank('signal',limit=3)==[]

def test_once_per_document_value_is_preserved():
 chunks=[Chunk('a','one','v',(),'signal'),Chunk('b','two','v',(),'other')]
 index=BM25Index(chunks);old=index._idf['signal'];correct_document_frequency(index)
 assert index._idf['signal']==old
