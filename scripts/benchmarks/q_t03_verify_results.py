"""Independently re-render captured Fast inputs and recheck frozen source data.

Use the HEAD export for before, the working tree for after. No LLM or input
repository code is executed. The original frozen evaluation script is unchanged.
"""
import argparse
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
from uuid import NAMESPACE_URL,uuid5

from scripts.benchmarks import q_t03_evidence_quality as bench


def verify(results,implementation):
    data=bench.read(results);receipt=bench.read(results.parent/'receipt.json')
    if bench.sha(results.read_bytes())!=receipt['results_sha256']:raise ValueError('Raw results receipt mismatch')
    protocol,questions,lock=bench.locked_inputs()
    if (data['protocol'],data['questions'],data['input_lock'])!=(protocol,questions,lock):raise ValueError('Frozen conditions differ')
    for path,expected in data['implementation']['files'].items():
        if bench.sha((implementation/path).read_bytes())!=expected:raise ValueError('Implementation bytes differ: '+path)
    sources=bench.corpus_sources(protocol)
    if {k:{p:bench.sha(s.encode()) for p,s in files.items()} for k,files in sources.items()}!=data['source_hashes']:
        raise ValueError('Frozen source identity differs')
    sys.path.insert(0,str(implementation))
    os.environ['PYTHON_DOTENV_DISABLED']='1'
    from pycode.prompt_builder import build_code_qa_prompt
    from pycode.retrieval_evidence import EvidenceItem,RetrievalResult,source_lines
    import pycode.prompt_builder as builder
    if not Path(builder.__file__).resolve().is_relative_to(implementation):raise ValueError('Wrong renderer loaded')
    truth={q['id']:q for q in questions};ref_count=0;prompt_count=0
    for row in data['rows']:
        items=[EvidenceItem(**item) for item in row['selected']]
        result=RetrievalResult(row['question'],row['intent'],items,status=row['status'],diagnostics=tuple(row['diagnostics']),
                               pack=SimpleNamespace(summary=lambda value=row['pack']:value))
        prompt=build_code_qa_prompt(result)
        if bench.sha(prompt.encode())!=row['fast_prompt_sha256']:raise ValueError('Captured Fast input cannot be reproduced: '+row['id'])
        if len(prompt.encode())!=row['cost']['fast_prompt_utf8_bytes']:raise ValueError('Prompt byte measurement differs')
        blocks=bench.metadata_blocks(prompt)
        if blocks!=row['fast_metadata_blocks']:raise ValueError('Captured metadata differs')
        fresh=bench.disclosure(row['selected'],blocks)
        if fresh!=row['relation_disclosure']:raise ValueError('Recorded disclosure does not match rendered input')
        row['relation_disclosure']=fresh
        visible=[dict(item.reference(),symbol_key=item.symbol_key) for item in items if item.snippet and item.snippet in prompt]
        if visible!=row['fast_visible']:raise ValueError('Fast source delivery observation differs')
        row['fast_visible']=visible
        corpus=row['corpus'];commit=protocol['corpora'][corpus].get('commit') or bench.sha(bench.canonical(sources[corpus]))[:40]
        binding=(str(uuid5(NAMESPACE_URL,'qt03-project-'+corpus)),str(uuid5(NAMESPACE_URL,'qt03-snapshot-'+corpus)),commit)
        checks=[]
        for item in row['selected']:
            lines=source_lines(sources[corpus][item['path']]);start,end=item['start_line'],item['end_line']
            valid=(item['project_id'],item['snapshot_id'],item['commit_sha'])==binding and type(start) is int and type(end) is int and 1<=start<=end<=len(lines)
            snippet='\n'.join(f'{n}: {lines[n-1]}' for n in range(start,end+1)) if valid else None
            checks.append(dict(path=item['path'],symbol=item['symbol'],start_line=start,end_line=end,address_valid=valid,source_consistent=valid and snippet==item['snippet']))
            ref_count+=1
        if checks!=row['reference_checks']:raise ValueError('Recorded source checks differ from pinned source blobs')
        row['reference_checks']=checks
        metrics=bench.score(row,truth[row['id']])
        if metrics!=row['metrics']:raise ValueError('Per-question quality metrics differ')
        prompt_count+=1
    summaries=bench.recompute(data)
    if summaries!=data['summaries']:raise ValueError('Summary does not recompute')
    return dict(result_sha256=receipt['results_sha256'],verified_fast_prompt_hashes=prompt_count,
                verified_source_references=ref_count,recomputed_relation_disclosure=summaries['overall']['fast_relation_provenance_disclosure'],
                regenerated_metrics_equal=True,actual_model_calls=0,
                limitation='Agent visible references and timing samples are recorded observations, not newly generated model executions.')


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--results',type=Path,required=True);p.add_argument('--implementation',type=Path,required=True)
    p.add_argument('--output',type=Path)
    args=p.parse_args();value=verify(args.results,args.implementation.resolve())
    if args.output:bench.write(args.output,value)
    print(json.dumps(value,indent=2))


if __name__=='__main__':main()
