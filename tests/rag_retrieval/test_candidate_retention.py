"""Regression coverage for DF correctness and bounded dense protection."""
import math
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from rag_retrieval.baseline import BM25Index, Chunk, reciprocal_rank_fusion
from rag_retrieval.candidates import LEGACY_POLICY, RETAIN_DENSE_POLICY, select_candidates
from rag_retrieval.enhancements import apply_source_boost, infer_source_ids

PROFILE = {"candidate_k": 200, "rrf_k": 40, "exact_weight": 1., "source_multiplier": 2.2}


def chunk(cid, source="aviation-official"):
    return Chunk(cid, source, "v7", (), "document")


class CandidateRetentionTests(unittest.TestCase):
    def select(self, dense, lexical, exact=(), query="aviation", policy=RETAIN_DENSE_POLICY,
               limit=150, chunks=None):
        chunks = chunks or {cid: chunk(cid) for cid in set(dense) | set(lexical) | set(exact)}
        return select_candidates(query=query, dense=dense, lexical=lexical, exact=exact,
                                 chunks_by_id=chunks, profile=PROFILE, policy=policy, limit=limit)

    def test_original_bm25_is_preserved_for_fusion_experiments(self):
        index = BM25Index([Chunk(str(i), "source", "v", (), "signal " * 10) for i in range(3)])
        self.assertAlmostEqual(index._idf["signal"], math.log(1+(3-30+.5)/(30+.5)))
        self.assertEqual(index.rank("signal", limit=3), [])

    def test_historical_df_correction_remains_isolated(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
        from evidence_bm25_counterfactual import correct_document_frequency
        a = BM25Index([Chunk("a", "source", "v", (), "signal"), chunk("b")])
        b = BM25Index([Chunk("a", "source", "v", (), "signal " * 100), chunk("b")])
        self.assertLess(b._idf["signal"], 0)
        correct_document_frequency(b)
        self.assertEqual(a._idf["signal"], b._idf["signal"])

    def test_dense100_survives_fusion_beyond200_and_source_boost(self):
        dense = [f"d{i}" for i in range(200)]
        lexical = [f"l{i}" for i in range(200)]
        chunks = {cid: chunk(cid, "scat-dataset-curated" if cid.startswith("l") else "official")
                  for cid in dense+lexical}
        selection = self.select(dense, lexical, lexical, query="I062/136", chunks=chunks)
        self.assertEqual(len(selection.fused_ids), 400)
        self.assertEqual(selection.ids[:100], tuple(dense[:100]))
        self.assertGreater(selection.signals["d99"]["fusion_rank"], 200)
        self.assertTrue(selection.signals["d99"]["protected_dense"])
        self.assertEqual(len(selection.ids), 150)

    def test_overlap_does_not_waste_extra_slots(self):
        dense = [f"d{i}" for i in range(200)]
        lexical = dense[:60]+[f"l{i}" for i in range(100)]
        selection = self.select(dense, lexical, lexical[:100])
        self.assertEqual(len(selection.ids), len(set(selection.ids)))
        self.assertEqual(len(selection.ids), 150)
        self.assertEqual(len(set(selection.ids)-set(dense[:100])), 50)

    def test_small_corpus_and_empty_optional_branches(self):
        selection = self.select(["a", "b"], [])
        self.assertEqual(selection.ids, ("a", "b"))
        self.assertEqual(selection.protected_ids, ("a", "b"))
        self.assertEqual(len(self.select(["a", "b"], [f"l{i}" for i in range(100)]).ids), 52)

    def test_old_policy_matches_original_formula(self):
        dense = [f"d{i}" for i in range(200)]
        lexical = [f"l{i}" for i in range(200)]
        chunks = {cid: chunk(cid) for cid in dense+lexical}
        expected = apply_source_boost(reciprocal_rank_fusion(
            [lexical, dense], rank_constant=40, weights=[1., 1.], limit=200),
            chunks, infer_source_ids("aviation"), 2.2)
        selection = self.select(dense, lexical, policy=LEGACY_POLICY, limit=100, chunks=chunks)
        self.assertEqual(selection.ids, tuple(cid for cid, _ in expected[:100]))

    def test_selection_is_deterministic(self):
        self.assertEqual(self.select(["a", "b"], ["b", "c"]),
                         self.select(["a", "b"], ["b", "c"]))

    def test_invalid_inputs_are_rejected(self):
        for kwargs in [{"limit": 100}, {"limit": True}, {"policy": "other"}, {"query": " "}]:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.select(["a"], ["b"], **kwargs)
        with self.assertRaises(ValueError):
            self.select(["a", "a"], [])
        with self.assertRaises(ValueError):
            self.select(["a"], [], chunks={"b": chunk("b")})


if __name__ == "__main__":
    unittest.main()
