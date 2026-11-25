import sys
from pathlib import Path
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'scripts'))
from complete_fusion_dense_control import adapt_saved_dense


def test_description_and_object_ranking_are_adapted():
    saved={'candidate_pool':'frozen BGE100 from main diagnostic',
           'ranking':[{'rank':1,'chunk_id':'b','score':.9},{'rank':2,'chunk_id':'a','score':.1}]}
    assert adapt_saved_dense(['a','b'],['a','b'],['a','b'],saved)==['b','a']


def test_ordered_candidate_identity_is_required():
    with pytest.raises(AssertionError):
        adapt_saved_dense(['b','a'],['a','b'],['a','b'],{'ranking':[]})


def test_saved_ranking_cannot_drop_or_duplicate_candidates():
    for ranking in [[{'chunk_id':'a'}],[{'chunk_id':'a'},{'chunk_id':'a'}]]:
        with pytest.raises(AssertionError):
            adapt_saved_dense(['a','b'],['a','b'],['a','b'],{'ranking':ranking})
