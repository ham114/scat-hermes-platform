"""Check the new ranking objective, preservation and unchanged candidate universe."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'scripts'))

import torch
from reranker_round2_run import lambda_loss, distill_loss


class Objectives(unittest.TestCase):
    def test_lambda_penalizes_reversing_evidence_order(self):
        correct=torch.tensor([3.,1.,-2.],requires_grad=True)
        reverse=torch.tensor([-2.,1.,3.],requires_grad=True)
        self.assertLess(lambda_loss(correct,[2,1,0]).item(),lambda_loss(reverse,[2,1,0]).item())
        lambda_loss(reverse,[2,1,0]).backward()
        self.assertLess(reverse.grad[0].item(),0)
        self.assertGreater(reverse.grad[2].item(),0)

    def test_lambda_ignores_equal_labels_and_is_finite(self):
        x=torch.tensor([1.,2.,3.],requires_grad=True)
        loss=lambda_loss(x,[1,1,1])
        self.assertEqual(loss.item(),0)
        loss.backward()
        self.assertTrue(torch.isfinite(x.grad).all())

    def test_lambda_topk_handles_more_than_ten_candidates(self):
        x=torch.arange(12,dtype=torch.float32,requires_grad=True)
        loss=lambda_loss(x,[2]+[0]*11,k=10)
        loss.backward()
        self.assertGreater(loss.item(),0)
        self.assertLess(x.grad[0].item(),0)

    def test_preservation_pulls_toward_original_probability(self):
        x=torch.tensor([2.,-1.],requires_grad=True)
        a=distill_loss(x,1,[.9,.1],[.2,.8],True)
        b=distill_loss(x,1,[.9,.1],[.2,.8],False)
        extra=a-b
        self.assertGreater(extra.item(),0)
        extra.backward()
        self.assertGreater(x.grad[0].item(),0)
        self.assertLess(x.grad[1].item(),0)

    def test_annotated_positive_overrides_teacher_false_negative(self):
        x=torch.tensor([-2.,2.],requires_grad=True)
        loss=distill_loss(x,1,[.001,.001],[.5,.5],False)
        loss.backward()
        self.assertTrue(torch.isfinite(loss))
        self.assertLess(x.grad[0].item(),0)
        self.assertGreater(x.grad[1].item(),0)


if __name__=='__main__':
    unittest.main()
