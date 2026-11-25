import importlib.util,sys,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'scripts'))
from reranker_ft_common import negative_allowed,tags,objective,norm

class NegativeContracts(unittest.TestCase):
 def check(self,**override):
  args=dict(candidate={'chunk_id':'n','text_sha256':'nsha','source_id':'s','section_path':['b'],'text':'unrelated'},protected_ids={'positive','dev','test'},protected_hashes={'same_positive_text'},positive_sections={('s',('a',))},query_tags={'weather'},candidate_tags={'surveillance'},positive_similarity=.2);args.update(override);return negative_allowed(**args)
 def test_no_positive_dev_or_test_as_negative(self):
  for cid in ['positive','dev','test']:
   self.assertFalse(self.check(candidate={'chunk_id':cid,'text_sha256':'nsha','source_id':'s','section_path':['b'],'text':'unrelated'}))
 def test_duplicate_positive_text_is_shielded(self):
  self.assertFalse(self.check(candidate={'chunk_id':'new_id','text_sha256':'same_positive_text','source_id':'z','section_path':['b'],'text':'unrelated'}))
 def test_weak_unknown_and_related_topics_not_negatives(self):
  self.assertFalse(self.check(candidate_tags=set()));self.assertFalse(self.check(candidate_tags={'weather','surveillance'}));self.assertFalse(self.check(positive_similarity=.81))
 def test_related_uas_fragments_are_not_negative(self):
  q=tags('CREWMEMBER UAS 人员定义');d=tags('UAS Pilot Certification and Requirements for Part 107')
  self.assertTrue(q&d);self.assertFalse(self.check(query_tags=q,candidate_tags=d))
 def test_late_attitude_content_is_in_topic_guard(self):
  q=tags('姿态仪的小型飞机如何调整');d=tags('Weather '*200+' Unusual Attitudes and Recoveries, attitude indicator')
  self.assertTrue(q&d)
 def test_oxygen_and_medical_fragments_share_human_topic(self):
  self.assertTrue(tags('座舱加热器头痛 carbon monoxide')&tags('Diluter-Demand Oxygen Systems'))
 def test_flight_plan_namespace_is_not_unrelated_to_other_flight_plans(self):
  self.assertTrue(tags('CAT062 Flight Plan Related Data')&tags('International Flight Plan, aircraft type'))
 def test_same_section_cannot_be_negative(self):
  self.assertFalse(self.check(positive_sections={('s',('b',))}))
 def test_disjoint_screened_document_can_be_negative(self):self.assertTrue(self.check())
 def test_question_normalization_is_stable(self):self.assertEqual(norm(' ＭＥＴＡＲ  18/16？'),norm('METAR18/16?'))

@unittest.skipUnless(importlib.util.find_spec('torch'),'torch not present in local test environment')
class ObjectiveContracts(unittest.TestCase):
 def test_multi_positive_both_gold_receive_upward_gradient(self):
  import torch
  x=torch.tensor([0.,0.,0.],requires_grad=True);objective(x,2,'multi_positive').backward()
  self.assertLess(x.grad[0],0);self.assertLess(x.grad[1],0);self.assertGreater(x.grad[2],0)
 def test_pairwise_raises_weakest_positive_and_lowers_negative(self):
  import torch
  x=torch.tensor([3.,-1.,1.,0.],requires_grad=True);objective(x,2,'pairwise',[2]).backward()
  self.assertLess(x.grad[1],0);self.assertGreater(x.grad[2],0)
 def test_positive_cannot_appear_in_hard_negative_positions(self):
  import torch
  with self.assertRaises(AssertionError):objective(torch.tensor([0.,0.,0.]),2,'pairwise',[1])
if __name__=='__main__':unittest.main()
