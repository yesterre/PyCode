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
export type RetrievalSource = 'exact_path' | 'exact_symbol' | 'keyword' | 'graph' | 'entry' | 'vector' | 'hybrid'
export type SourceScore = { source: 'keyword' | 'vector' | 'graph'; score: number; rank: number; contribution: number }
export type EvidencePack = { budget: number; estimated_tokens: number; estimator: string; selected_count: number;
  discarded_count: number; discarded: {path: string; symbol: string | null; start_line: number | null;
    end_line: number | null; reason: string}[]; discarded_omitted: number; reason_counts: Record<string, number> }
export type RetrievalSummary = {
  strategy: string; status: 'ok' | 'degraded' | 'empty' | 'unavailable';
  binding: {project_id: string; snapshot_id: string; commit_sha: string} | null;
  diagnostics: string[]; duration_ms: number; evidence_count: number;
  references: {path: string | null; symbol: string | null; start_line: number | null;
    end_line: number | null; chunk_ids: string[]; source: RetrievalSource; score: number;
    reason: string; role: string; coverage: string; diagnostics: string[]; source_scores?: SourceScore[];
    graph_path?: {source: string; target: string; relation: string; direction: 'incoming' | 'outgoing';
      resolution?: string | null; basis?: string | null; site_id?: string | null; path?: string | null;
      start_line?: number | null; end_line?: number | null}[]}[];
  mode?: 'keyword' | 'vector' | 'hybrid'; pack?: EvidencePack | null; graph_stats?: Record<string, number | boolean> | null;
  graph_schema_version?: number | null
}
export type Analysis = { project_id: string; answer: string; intent: string; evidence: string[];
  retrieval?: RetrievalSummary | null }
export type AnalysisHistoryMode = 'repository' | 'impact' | 'agent'
export type AnalysisHistoryItem = {
  id: string; created_at: string; mode: AnalysisHistoryMode; question: string; answer: string
}
export type AnalysisHistoryResponse = {items: AnalysisHistoryItem[]; has_more: boolean}
export type AgentRun = {
  id: string; project_id: string; snapshot_id: string | null; task: string; run_type: string;
  status: TaskStatus; result: string | null; error_message: string | null;
  created_at: string; started_at: string | null; core_started_at: string | null; finished_at: string | null
  request_metadata?: {schema_version: 1; binding: RunBinding; entrypoint: 'ask'|'impact'|'agent'} |
    {schema_version: 2; binding: RunBinding; entrypoint: 'question'; target: string | null} | null
  execution_metadata?: {schema_version: 1; binding: RunBinding; strategy: 'fast'|'agent'; source: 'legacy_entrypoint'} |
    (QuestionExecution & ({schema_version: 2} | {schema_version: 3; initial_strategy: 'fast'; preflight: QuestionPreflight})) | null
  result_metadata?: RunResultMetadata | null
  metadata_warnings?: string[]
}
export type RunBinding = {project_id: string; snapshot_id: string; commit_sha: string}
export type QuestionExecution = {binding: RunBinding; entrypoint: 'question'; strategy: 'fast'|'agent'; source: 'rules';
  policy_version: 'a04b-v1'; intent: string; target: string | null; reasons: string[]; evidence_requirement: 'snapshot_evidence'|'multi_step'}
export type QuestionPreflight = {status:'sufficient'|'insufficient'|'failed'; reasons:string[]; pack_digest:string|null;
  selected_count:number; discarded_count:number; budget:number|null; estimated_tokens:number|null}
export type RunResultMetadata = {
  schema_version: 1; binding: RunBinding; outcome: 'completed'|'failed'
  quality: 'evidence_available'|'partial'|'insufficient'|'unavailable'; intent: string | null
  references: {path: string; symbol: string | null; start_line: number | null; end_line: number | null;
    chunk_ids: string[]; chunk_ids_omitted: number; source: RetrievalSource;
    coverage: 'complete_symbol'|'complete_file'|'unavailable'; relation_resolution: 'static'|'declaration'|'legacy'|'unknown'|null}[]
  references_omitted: number; diagnostics: string[]
}
export type EventType = 'run_started' | 'plan_created' | 'context_updated' | 'llm_started' |
  'llm_finished' | 'tool_started' | 'tool_finished' | 'run_completed' | 'run_failed'
export type TraceEvent = {
  schema_version: 1; sequence: number; event_type: EventType; occurred_at: string;
  tool_name: string | null; payload: Record<string, unknown>
}
export type HarnessBudget = {
  schema_version: 1;
  limits: {max_turns:number; max_tool_calls:number; max_model_calls:number; max_query_embeddings:number;
    max_runtime_seconds:number; context_tokens:number; tool_output_chars:number};
  usage: {turns:number; tool_calls:number; model_calls:number; query_embeddings:number};
  elapsed_ms:number; stop_reason:string|null;
  context_output_enforcement:'reserved_for_a05c'; transport_attempts:'not_observed';
}
export type TraceHistory = { run_id: string; events: TraceEvent[] }
export type GraphNode = { id: string; type: string; name: string; path: string | null;
  symbol_key?: string | null; parent_id?: string | null; start_line?: number | null; end_line?: number | null }
export type GraphEdge = { source: string; target: string; type: string; resolution?: string | null;
  basis?: string | null; site_id?: string | null; path?: string | null; start_line?: number | null; end_line?: number | null }
export type GraphResult = { snapshot_id: string; nodes: GraphNode[]; edges: GraphEdge[]; truncated: boolean;
  graph_schema_version?: number; graph_capabilities?: string[]; graph_warnings?: string[];
  call_sites?: {id: string; path: string; caller_id: string; start_line: number; end_line: number;
    resolution: string; basis: string; target_id: string | null; candidate_count: number;
    binding_declarations?: string[]; binding_chain_omitted?: number;
    flow_evidence?: string[]; flow_evidence_omitted?: number}[]; call_sites_omitted?: number }
