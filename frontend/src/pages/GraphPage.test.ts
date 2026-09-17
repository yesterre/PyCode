import { expect, it } from 'vitest'
import { graphElements } from './GraphPage'

it('maps external nodes and contains/imports/calls edges to Cytoscape', () => {
  const elements = graphElements({snapshot_id: 'snapshot', truncated: true,
    nodes: [{id: 'file:a.py', type: 'file', name: 'a.py', path: 'a.py'},
      {id: 'external:os', type: 'external', name: 'os', path: null}],
    edges: [{source: 'file:a.py', target: 'external:os', type: 'imports'}]})
  expect(elements[1].data.path).toBeNull()
  expect(elements[2].data).toMatchObject({source: 'file:a.py', target: 'external:os', type: 'imports'})
})
