# Q-T03 frozen evidence evaluation

`protocol.json`, `questions.json`, `annotation_review.json` and source fixtures
were authored before the successful HEAD baseline and production modification.
`lock.json` records their exact byte hashes and the evaluation script hash.
The corpus has 40 questions, 56 necessary evidence units, and 12 validation
questions whose outputs are not inspected during the implementation decision.

Eight questions reuse A00 topics at its original fixed Commit. Their independent
minimal statement labels are described in annotation_review.json; neither A00 nor
A07 data, receipts or implementation fingerprints are changed. Twenty-eight
questions use the complete itsdangerous package at the exact acceptance Commit;
source bytes/license/provenance are reused read-only from the F-T05 fixture.
Four small fixtures cover imported bindings, homonyms, unknown receivers and a
legacy Graph. Source programs are parsed, never executed or imported.

Annotations are source-reviewed by the development agent and independent of
Retriever predictions. They have not been adjudicated by an external human.
Minimal ranges, allowed full-symbol alternatives, facts and uncertain runtime
relations are explicit. Necessary-evidence precision is a closed-label proxy;
it must not be presented as exhaustive semantic relevance precision.

The primary change metric is per-step Graph provenance disclosure in the Fast
rendered input: an existing relation's source, target, kind, recorded resolution,
basis and runtime-unverified qualifier must survive rendering. This is distinct
from Graph soundness, relation semantic correctness, generated citation semantics
and final answer accuracy. Missing proof is not silently treated as static.

The benchmark renders the actual Fast prompt and production Agent summary
context, records selected/omitted code references, validates source bytes and
Snapshot binding, and measures retrieval/Pack cost. No model client or substitute
answer is used. Therefore actual model delivery, generated-answer citations,
semantic entailment, billed tokens and final answer quality are not_evaluated.
UTF-8 byte budgets are not actual model token usage.

Run `python -m scripts.benchmarks.q_t03_evidence_quality --help`; commands and
before/after raw results are documented in
`docs_v3.0/Q-T03-01_rag_evidence_quality_improvement.md`. Output directories must
be new. Results can be re-scored from the raw rows and frozen labels. Labels are
passed only to post-run observers, never to retrieval or prompt assembly.
