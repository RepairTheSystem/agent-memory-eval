"""Recompute historical metrics from per-case binary verdicts without network access."""
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from memeval.stats import mcnemar, wilson


def recompute(path):
    doc = json.loads(path.read_text(encoding='utf-8'))
    rows = doc['cases']
    if not rows:
        raise ValueError(f'{path.name}: no verdicts')
    keys = [(r['id'], r.get('repeat', 0)) for r in rows]
    if len(keys) != len(set(keys)):
        raise ValueError(f'{path.name}: duplicate case/repeat')
    result = {}
    for variant in doc['variants']:
        verdicts = [r['correct'][variant] for r in rows]
        if not all(type(v) is bool for v in verdicts):
            raise ValueError(f'{path.name}: non-boolean verdict')
        n, correct = len(verdicts), sum(verdicts)
        expected = doc['summary'][variant]
        if expected['n'] != n or expected.get('correct', expected.get('passed')) != correct:
            raise ValueError(f'{path.name}: count mismatch for {variant}')
        if abs(expected['accuracy'] - correct/n) > 1e-12:
            raise ValueError(f'{path.name}: accuracy mismatch for {variant}')
        interval = wilson(correct, n)
        saved = expected.get('ci', expected.get('ci95'))
        if saved is not None and any(abs(a-b) > 0.00051 for a,b in zip(saved, interval)):
            raise ValueError(f'{path.name}: confidence interval mismatch for {variant}')
        result[variant] = {'n':n,'correct':correct,'accuracy':correct/n,'ci95':interval}
    base = doc['variants'][0]
    comparisons = {v:mcnemar([(r['correct'][base],r['correct'][v]) for r in rows])
                   for v in doc['variants'][1:]}
    return result, comparisons


def main():
    paths = sorted((ROOT / 'results/historical').glob('*.json'))
    if len(paths) != 4:
        raise ValueError('Expected four archived runs')
    for path in paths:
        summary, pairs = recompute(path)
        print(path.name)
        for variant,s in summary.items():
            low,high = s['ci95']
            print(f"  {variant}: {s['correct']}/{s['n']} = {s['accuracy']:.2%}; 95% CI [{low:.2%}, {high:.2%}]")
        for variant,p in pairs.items():
            print(f"  before -> {variant}: fixed={p['fixed']}, broken={p['broken']}, p={p['p_value']:.3g}")
    print('All archived counts and intervals verified. Model judgments were not re-evaluated.')


if __name__ == '__main__':
    main()
