# F-T05-01 frozen receiver evaluation

The primary evaluation oracle is `cases.reviewed.json`: 72 isolated source-as-data
cases, 48 unique-target labels and 24 genuinely uncertain labels. Six existing
receiver labels bring it to 78 labelled calls (54 unique targets, 24 uncertain).
All 153 calls are recorded; the 75 unlabelled calls never enter accuracy metrics.
`annotation_review.json` openly records eight initial oracle mistakes and their
source-based corrections. Fixed inheritance, literal getattr, final unconditional
assignment and captured/dead closures with unique targets are counted as recall
misses, even though the current analyzer deliberately abstains. Eight genuine
uncertainty cases were added. The original V1 files/results were never overwritten.
The review happened after the initial implementation; both implementations were
then evaluated on exactly the same reviewed data. Production code did not change.

`cases.json` preserves the initial 64-case behavior contract, not the final quality
oracle. Its original 40-positive/24-abstention split overestimated recall because
eight abstentions actually have source-determinate targets. Each label specifies an
expected target or mandatory uncertainty and a five-part source location:
path, lexical scope, line, column, expression. Expectations were authored before
running either implementation; locations were derived from Python AST only.
Twelve positives deliberately exercise variants of one narrow identity-projection rule;
their measured gain must not be treated as a real-world prevalence estimate.

Six existing labels from `tests/fixtures/a03/receivers` brought the initial V1
cohort to 70 labels. The original 100% post-change V1 recall must not be used as
the quality claim; reviewed recall is 46/54 = 85.19%, with eight honest misses.
Synthetic classes are closed examples with no hidden patches/subclasses. This is
the existing nominal model, not a proof about every possible Python runtime.

`itsdangerous/` vendors all eight Python package modules from the exact upstream
commit in `PROVENANCE.json`; byte hashes and the original BSD license are retained.
The repository's source is never imported or executed. `real_labels.json` has
17 manually reviewed sites in signer.py: 11 direct imported encoding calls with
unique source targets and six open receiver dispatches that must stay uncertain.
The other 146 package calls lack ground truth and are excluded from accuracy.
The open library contract explicitly permits subclasses and method customization;
no particular runtime receiver is proved for those six sites. Existing nominal
predictions that violate this contract count as false positives, not successes.

`legacy_v4.json` was produced by HEAD
`266223c833514af7d414bbf31e527b4b994ce786` before modifying production code. It keeps
the conservative `receiver_escaped` result and must round-trip unchanged under
the improved loader. It is a small compatibility fixture, not a user Snapshot.

Run the primary oracle with `scripts.benchmarks.f_t05_graph_reviewed` (a fixture
adapter over the unchanged `f_t05_graph_resolution` timing/metrics harness).
Run V1 with `scripts.benchmarks.f_t05_graph_resolution`; the full
methodology and reproduction commands are in
`docs_v3.0/F-T05-01_graph_resolution_improvement.md`. Before/after raw JSON includes
all expectations, predictions, distributions, individual timing samples, Python
allocation peaks, implementation file hashes and dataset fingerprint.
