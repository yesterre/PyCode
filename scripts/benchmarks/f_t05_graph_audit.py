"""Inspect persisted binding/escape evidence for the pinned derive_key cases."""
import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[2]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--implementation', type=Path, default=ROOT)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    sys.path.insert(0, str(args.implementation.resolve()))
    from pycode.engine import PyCodeEngine
    from pycode.graph_builder import build_code_graph
    from pycode.symbol_resolver import SymbolResolver

    root = ROOT/'tests/fixtures/f_t05/itsdangerous/src'
    index = PyCodeEngine().build_index(root, include_graph_metadata=True)
    graph = build_code_graph(index)
    resolver = SymbolResolver(graph.nodes,graph.scopes,graph.import_declarations,
                              graph.binding_facts,graph.flow_facts)
    scopes = {s['id']:s for s in graph.scopes}
    calls = {s['id']:s for s in graph.call_sites}
    flow = next(f for f in graph.flow_facts if f['path']=='itsdangerous/signer.py')
    records = []
    for site in graph.call_sites:
        if site['path'] != 'itsdangerous/signer.py' or site['expression'] != 'self.derive_key':
            continue
        sid = site['scope_id']
        records.append(dict(site=site, scope=scopes[sid], escaped_names=sorted(resolver.escaped.get(sid,())),
            escape_facts=[dict(fact=e, expression=calls[e['call_id']]['expression'],
                               line=calls[e['call_id']]['start_line']) for e in flow['escapes'] if e['scope_id']==sid],
            receiver_metadata=resolver.methods[sid],
            receiver_bindings=resolver.bindings[sid].get('self'),
            class_flags=resolver.flags['class:itsdangerous/signer.py:Signer'],
            candidate_definition=asdict(resolver.nodes['method:itsdangerous/signer.py:Signer.derive_key']),
            visible_method_writes=resolver._writes('class:itsdangerous/signer.py:Signer','derive_key')))
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(records,indent=2)+'\n',encoding='utf-8')
    print(json.dumps([(r['site']['start_line'],r['site']['resolution'],r['escaped_names']) for r in records]))


if __name__ == '__main__':
    main()
