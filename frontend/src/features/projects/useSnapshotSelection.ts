import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { getSnapshot, listSnapshots } from '../../api/snapshots'

export function useSnapshotSelection(projectId: string, currentId?: string | null) {
  const [selection, setSelection] = useState<{projectId: string; id: string} | null>(null)
  const snapshotId = selection?.projectId === projectId ? selection.id : currentId
  const snapshots = useQuery({queryKey: ['snapshots', projectId], queryFn: () => listSnapshots(projectId)})
  const snapshot = useQuery({queryKey: ['snapshot', projectId, snapshotId],
    queryFn: () => getSnapshot(projectId, snapshotId!), enabled: !!snapshotId,
    refetchInterval: query => query.state.data?.status === 'indexing' ||
      ['queued', 'building'].includes(query.state.data?.semantic_index?.status ?? '') ? 2000 : false})
  const ready = !!snapshot.data && snapshot.data.project_id === projectId && snapshot.data.id === snapshotId &&
    snapshot.data.status === 'ready' && !snapshot.isError
  return {snapshotId, snapshots, snapshot, ready,
    selectSnapshot: (id: string) => setSelection(id === currentId ? null : {projectId, id})}
}
