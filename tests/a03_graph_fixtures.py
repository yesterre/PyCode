"""Older Schema3 producer semantics, for compatibility tests and fixed comparisons.

Replays the unchanged import-only resolver on the same AST facts. No fixture
source is executed and no golden A00/A02 data is changed.
"""
from pycode.graph_schema import graph_to_dict,graph_from_dict,node_data
from pycode.models import GraphNode
from pycode.symbol_resolver import SymbolResolver,dotted


def schema3(graph):
    data=graph_to_dict(graph);data['schema_version']=3;data.pop('flow_facts')
    data['producer']='pycode-a03b-v1';data['capabilities']=[c for c in data['capabilities'] if c!='local_receiver_bindings']
    resolver=SymbolResolver(graph.nodes,graph.scopes,graph.import_declarations,graph.binding_facts)
    nodes={n['id']:n for n in data['nodes']};edges=[e for e in data['edges'] if e['type'] in {'contains','imports'}]
    for site in data['call_sites']:
        site.pop('flow_chain');site.update(resolver.resolve(site));target=site['target_id']
        if target is None:
            target='unresolved:'+site['id'];parts=dotted(site['expression'])
            nodes[target]=node_data(GraphNode(target,'external',parts[0] if len(parts)==1 else '<dynamic call>'),details=True)
        metadata={k:site[k] for k in ('path','start_line','end_line','resolution','basis')}
        edges.append(dict(source=site['caller_id'],target=target,type='calls' if site['resolution']=='static' else 'unresolved_call',site_id=site['id'],**metadata))
        if site['resolution']=='static' and site['binding_chain']:
            edges.append(dict(source=site['caller_id'],target=target,type='references',site_id=site['id'],**metadata))
    data['nodes']=sorted(nodes.values(),key=lambda n:n['id'])
    data['edges']=sorted(edges,key=lambda e:(e['source'],e['target'],e['type'],e['site_id'] or ''))
    return graph_from_dict(data)
