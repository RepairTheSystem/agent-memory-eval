import json
from pathlib import Path
import unittest

from memeval.live import load_scenarios
from scripts.verify_results import recompute

ROOT = Path(__file__).resolve().parents[1]


class HistoricalResults(unittest.TestCase):
    def test_saved_counts_match_case_verdicts(self):
        expected = {'live-qwen.json':(248,121,232),'live-qwen38.json':(248,120,224),
                    'lme-qwen.json':(500,128,316),'lme-qwen38.json':(500,142,295)}
        for name,(n,before,search) in expected.items():
            with self.subTest(name=name):
                summary,pairs = recompute(ROOT/'results/historical'/name)
                self.assertEqual(summary['before']['n'],n)
                self.assertEqual(summary['before']['correct'],before)
                self.assertEqual(summary['search']['correct'],search)
                self.assertGreater(pairs['search']['fixed'],pairs['search']['broken'])

    def test_historical_dataset_is_255_with_248_measured(self):
        rows = load_scenarios(ROOT/'scenarios/benchmark_255.json')
        self.assertEqual(len(rows),255)
        self.assertEqual(sum(r.get('measure',True) for r in rows),248)
        self.assertEqual(len({r['id'] for r in rows}),len(rows))

    def test_extended_dataset_is_separate(self):
        rows = load_scenarios(ROOT/'scenarios/extended_302.json')
        self.assertEqual(len(rows),302)

    def test_historical_case_ids_match_dataset(self):
        rows = load_scenarios(ROOT/'scenarios/benchmark_255.json')
        ids = {r['id'] for r in rows if r.get('measure',True)}
        for name in ['live-qwen.json','live-qwen38.json']:
            doc = json.loads((ROOT/'results/historical'/name).read_text(encoding='utf-8'))
            self.assertEqual({r['id'] for r in doc['cases']},ids)


if __name__ == '__main__':
    unittest.main()
