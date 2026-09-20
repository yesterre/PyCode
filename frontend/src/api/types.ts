export type ProjectStatus = 'created' | 'preparing' | 'indexing' | 'ready' | 'failed'
export type TaskStatus = 'queued' | 'running' | 'completed' | 'failed'
export type IndexSummary = { file_count: number; node_count: number; edge_count: number }
export type Project = {
  id: string; name: string; repo_url: string; branch: string | null;
  status: ProjectStatus; current_snapshot_id: string | null;
  created_at: string; updated_at: string; index_summary: IndexSummary | null; last_error: string | null
}
export type Snapshot = {
  id: string; project_id: string; commit_sha: string; status: 'indexing' | 'ready' | 'failed';
  file_count: number | null; node_count: number | null; edge_count: number | null;
  created_at: string; updated_at: string; error_message: string | null
}
export type BackgroundTask = {
  id: string; task_type: string; status: TaskStatus; project_id: string | null;
  snapshot_id: string | null; agent_run_id: string | null; error_code: string | null;
  error_message: string | null; created_at: string; started_at: string | null; finished_at: string | null
}
export type Analysis = { project_id: string; answer: string; intent: string; evidence: string[] }
export type AnalysisHistoryMode = 'repository' | 'impact' | 'agent'
export type AnalysisHistoryItem = {
  id: string; created_at: string; mode: AnalysisHistoryMode; question: string; answer: string
}
export type AnalysisHistoryResponse = {items: AnalysisHistoryItem[]; has_more: boolean}
export type AgentRun = {
  id: string; project_id: string; snapshot_id: string | null; task: string; run_type: string;
  status: TaskStatus; result: string | null; error_message: string | null;
  created_at: string; started_at: string | null; core_started_at: string | null; finished_at: string | null
}
export type EventType = 'run_started' | 'plan_created' | 'context_updated' | 'llm_started' |
  'llm_finished' | 'tool_started' | 'tool_finished' | 'run_completed' | 'run_failed'
export type TraceEvent = {
  schema_version: 1; sequence: number; event_type: EventType; occurred_at: string;
  tool_name: string | null; payload: Record<string, unknown>
}
export type TraceHistory = { run_id: string; events: TraceEvent[] }
export type GraphNode = { id: string; type: string; name: string; path: string | null }
export type GraphEdge = { source: string; target: string; type: string }
export type GraphResult = { snapshot_id: string; nodes: GraphNode[]; edges: GraphEdge[]; truncated: boolean }
