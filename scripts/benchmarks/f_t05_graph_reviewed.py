"""Run the unchanged F-T05 harness with the openly adjudicated V2 oracle.

The V1 dataset, harness and raw runs remain available. Eight unsupported but
source-determinate V1 examples are recall misses here, not successful abstentions.
Only the fixture provider changes; build, metrics, timer and memory code are the
same for both HEAD and working-tree evaluation.
"""
from scripts.benchmarks import f_t05_graph_resolution as harness


def main():
    original = harness.fixtures

    def reviewed_fixtures():
        data = original()
        existing = next(c for c in data['synthetic'] if c['id']=='existing-receivers')
        data['synthetic'] = harness.read_json(harness.FIXTURES/'cases.reviewed.json')['cases'] + [existing]
        return data

    harness.fixtures = reviewed_fixtures
    harness.main()


if __name__ == '__main__':
    main()
