export type DataSourceType = "csv" | "sqlite" | "mysql";
export type DataSourceStatus =
  | "uploaded"
  | "inspecting"
  | "schema_ready"
  | "building_datalink"
  | "ready"
  | "failed"
  | "deleting"
  | "deleted";
export type ModelProfileStatus = "created" | "tested" | "failed";
export type MessageRole = "user" | "assistant";
export type RunStatus = "queued" | "running" | "succeeded" | "failed" | "canceled";
export type CompletionKind = "completed" | "partial" | "clarification";
export type RunProtocolId = "general-task" | "data-analysis";
export type ArtifactType = "table" | "chart" | "markdown" | "file";
export type FinalOutputMode = "markdown" | "json_schema" | "json_object" | "submit_answer";

export interface PageResult<T> {
  items: T[];
  total: number;
  page: number;
  page_size: number;
}

export interface DeleteResult {
  deleted: boolean;
}

export interface Session {
  id: string;
  title: string;
  selected_datasource_id: string | null;
  created_at: string;
  updated_at: string;
  last_message_at: string | null;
}

export interface SessionCreate {
  title: string;
  selected_datasource_id?: string | null;
}

export interface SessionUpdate {
  title?: string | null;
  selected_datasource_id?: string | null;
}

export interface Message {
  id: string;
  session_id: string;
  run_id: string | null;
  role: MessageRole;
  content_text: string;
  answer_evidence_refs: string[] | null;
  position: number;
  created_at: string;
}

export interface RunCreate {
  question: string;
  idempotency_key: string;
}

export interface RunCreateAccepted {
  run_id: string;
  session_id: string;
  status: RunStatus;
}

export interface Run {
  id: string;
  session_id: string;
  datasource_id: string | null;
  datasource_deleted: boolean;
  user_message_id: string | null;
  question: string;
  status: RunStatus;
  protocol_id: RunProtocolId | null;
  model_profile_id: string | null;
  model_provider: string | null;
  model_name: string | null;
  schema_revision: number | null;
  datalink_graph_version: string | null;
  run_timeout_seconds: number | null;
  completion_kind: CompletionKind | null;
  incomplete_reason: string | null;
  error_code: string | null;
  error_message: string | null;
  cancel_requested_at: string | null;
  cancel_reason: string | null;
  started_at: string | null;
  finished_at: string | null;
  created_at: string;
  updated_at: string;
  answer_data_freshness?: "not_queried" | "current_schema" | "current_run_observation_only" | "current_run_evidence" | null;
  historical_context_injected?: boolean;
  historical_summary_count?: number;
}

export interface RunCancelRequest {
  reason?: string;
}

export interface RunCancel {
  run_id: string;
  status: RunStatus;
  cancel_requested_at: string | null;
}

export type Scalar = string | number | boolean | null;
export type ToolInputValue = Scalar | string[];

export interface ToolCall {
  id: string;
  run_id: string;
  tool_name: string;
  status: string;
  input_params: Record<string, ToolInputValue> | null;
  output_summary: Record<string, Scalar> | null;
  error_code: string | null;
  error_message: string | null;
  started_at: string;
  finished_at: string | null;
}

export interface SqlAudit {
  id: string;
  run_id: string | null;
  tool_call_id: string | null;
  datasource_id: string | null;
  datasource_deleted: boolean;
  schema_revision: number | null;
  attempt_no: number;
  repaired_from_id: string | null;
  original_sql: string;
  normalized_sql: string | null;
  status: string;
  statement_type: string | null;
  referenced_tables: string[];
  blocked_reason_code: string | null;
  blocked_reason: string | null;
  artifact_id: string | null;
  row_count: number | null;
  elapsed_ms: number | null;
  error_code: string | null;
  error_message: string | null;
  created_at: string;
  updated_at: string;
}

export interface RunArtifact {
  id: string;
  run_id: string | null;
  session_id: string | null;
  tool_call_id: string | null;
  datasource_deleted: boolean;
  type: ArtifactType;
  title: string;
  mime_type: string;
  size_bytes: number;
  inline_previewable: boolean;
  preview: Record<string, unknown> | null;
  metadata: Record<string, unknown> | null;
  content_hash: string;
  created_at: string;
}

export type TraceDagNodeKind =
  | "run-start"
  | "preparation"
  | "agent-turn"
  | "tool"
  | "artifact"
  | "final-answer"
  | "run-terminal";
export type TraceDagEdgeKind = "starts" | "continues" | "invokes" | "produces_artifact" | "completes";
export type TraceDagRelationshipStatus = "resolved" | "unresolved";
export type TraceDagActionKind =
  | "run_started"
  | "protocol_selected"
  | "preparation_started"
  | "preparation_completed"
  | "turn_started"
  | "turn_completed"
  | "tool_requested"
  | "tool_completed"
  | "discovery_observed"
  | "artifact_registered"
  | "claim_committed"
  | "answer_requested"
  | "answer_validated"
  | "terminal_recorded";

export interface TraceDagActionRecord {
  id: string;
  kind: TraceDagActionKind;
  event_seq: number;
  status: string | null;
  label: string;
  summary: string | null;
  tool_call_id: string | null;
  artifact_id: string | null;
  reason: string | null;
}

export interface TraceDagNode {
  id: string;
  kind: TraceDagNodeKind;
  run_id: string;
  label: string;
  start_seq: number | null;
  end_seq: number | null;
  status: string | null;
  summary: string | null;
  turn_no: number | null;
  tool_call_id: string | null;
  artifact_id: string | null;
  detail_event_seq: number | null;
  relationship_status: TraceDagRelationshipStatus;
  action_records: TraceDagActionRecord[];
  detail: Record<string, string | number | boolean | null>;
}

export interface TraceDagEdge {
  id: string;
  source: string;
  target: string;
  kind: TraceDagEdgeKind;
  label: string | null;
}

export interface TraceDagSection {
  id: string;
  title: string;
  status: string | null;
  start_seq: number;
  end_seq: number;
  node_ids: string[];
}

export interface TraceDag {
  run_id: string;
  nodes: TraceDagNode[];
  edges: TraceDagEdge[];
  sections: TraceDagSection[];
  warnings: string[];
}

export interface DataSourceTypeDescriptor {
  type: DataSourceType;
  label: string;
  description: string;
  enabled: boolean;
  accepted_extensions: string[];
  dialect: string;
  upload_mode: "file" | "connection";
  parameters?: Array<{
    name: string;
    label: string;
    type: "string" | "integer" | "boolean";
    required: boolean;
    default: string | number | boolean | null;
    secret: boolean;
  }>;
  capabilities?: Record<string, boolean>;
}

export interface DataSourceDescriptionUpdate {
  description: string | null;
}

export interface DataSource {
  id: string;
  name: string;
  description: string | null;
  type: DataSourceType;
  status: DataSourceStatus;
  schema_revision: number;
  connection_revision?: number;
  source_kind?: "file" | "connection";
  has_credentials?: boolean;
  connection_summary?: { host: string; port: number; database: string; username?: string; tls: boolean } | null;
  mask_fields: string[];
  mask_fields_confirmed: boolean;
  schema: SchemaSummary | null;
  datalink_build_id: string | null;
  datalink_graph_version: string | null;
  last_error_code: string | null;
  last_error_message: string | null;
  last_test_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface SchemaColumn {
  name: string;
  type: string;
  nullable: boolean;
}

export interface ForeignKey {
  columns: string[];
  referenced_table: string;
  referenced_columns: string[];
}

export interface SchemaTable {
  name: string;
  columns: SchemaColumn[];
  row_count: number | null;
  primary_key: string[];
  foreign_keys: ForeignKey[];
}

export interface SchemaSummary {
  datasource_id: string;
  dialect: string;
  tables: SchemaTable[];
}

export interface TableData {
  columns: string[];
  rows: Scalar[][];
  row_count: number;
}

export interface DataSourceDeleteResult {
  datasource_id: string;
  status: DataSourceStatus;
}

export type DataLinkBuildStatus = "running" | "completed" | "failed";
export type DataLinkProvenance = "structural" | "database_foreign_key" | "inferred_candidate" | "semantic_mapping" | "manual" | "unknown";
export type DataLinkNodeType = "table" | "column" | "concept" | "entity";
export type DataLinkGraphEntryType = "table" | "entity";
export type DataLinkEdgeType =
  | "contains"
  | "foreign_key"
  | "joinable"
  | "semantic_synonym"
  | "represents"
  | "has_concept"
  | "distribution_similar"
  | "correlated";

export interface DataLinkColumnProfile {
  column_id: string;
  dtype: string;
  semantic_type: string | null;
  null_rate: number;
  distinct_count: number;
  unique_rate: number;
  min_value: Scalar;
  max_value: Scalar;
  top_values: Scalar[];
  sample_values: Scalar[];
}

export interface DataLinkNode {
  provenance?: DataLinkProvenance;
  id: string;
  type: DataLinkNodeType;
  name: string;
  table: string | null;
  semantic_type: string | null;
  description: string | null;
  aliases: string[];
  profile: DataLinkColumnProfile | null;
}

export interface DataLinkEdgeEvidence {
  kind: string;
  summary: string;
}

export interface DataLinkEdge {
  provenance?: DataLinkProvenance;
  enabled?: boolean;
  source: string;
  target: string;
  type: DataLinkEdgeType;
  confidence: number | null;
  evidence: DataLinkEdgeEvidence | null;
}

export interface DataLinkJoinPathStep {
  provenance?: DataLinkProvenance;
  source_table: string;
  source_column: string;
  target_table: string;
  target_column: string;
  edge_type: "foreign_key" | "joinable";
  confidence: number | null;
  evidence: DataLinkEdgeEvidence | null;
}

export interface DataLinkJoinPath {
  provenance?: DataLinkProvenance;
  tables: string[];
  steps: DataLinkJoinPathStep[];
  confidence: number | null;
  evidence: string | null;
}

export interface DataLinkGraph {
  datasource_id: string;
  graph_version: string | null;
  nodes: DataLinkBrowserNode[];
  edges: DataLinkEdge[];
  warnings: string[];
}

export interface DataLinkGraphEntry {
  id: string;
  type: DataLinkGraphEntryType;
  name: string;
  description: string | null;
  aliases: string[];
}

export interface DataLinkGraphEntries {
  datasource_id: string;
  graph_version: string;
  items: DataLinkGraphEntry[];
  page: number;
  page_size: number;
  total: number;
}

export interface DataLinkBrowserProfile {
  dtype: string;
  semantic_type: string | null;
  null_rate: number;
  distinct_count: number;
  unique_rate: number;
}

export interface DataLinkBrowserNode {
  provenance?: DataLinkProvenance;
  id: string;
  type: DataLinkNodeType;
  name: string;
  table: string | null;
  semantic_type: string | null;
  description: string | null;
  aliases: string[];
  profile: DataLinkBrowserProfile | null;
}

export interface DataLinkSubgraph {
  datasource_id: string;
  graph_version: string;
  root_node_id: string;
  total_node_count: number;
  total_edge_count: number;
  nodes: DataLinkBrowserNode[];
  edges: DataLinkEdge[];
  is_truncated: boolean;
  warnings: string[];
}

export interface DataLinkBuild {
  build_id: string;
  datasource_id: string;
  schema_revision: number;
  attempt_no: number;
  status: DataLinkBuildStatus;
  graph_version: string | null;
  error_code: string | null;
  error_message: string | null;
  started_at: string | null;
  finished_at: string | null;
  created_at: string | null;
}

export interface DataLinkAutomaticSemantics {
  name?: string | null;
  description?: string | null;
  aliases: string[];
  semantic_type?: string | null;
}

export interface DataLinkCatalogPrimaryMapping {
  concept_name: string;
  concept_description?: string | null;
  entity_name?: string | null;
  field_to_concept_confidence: number | null;
  provenance: DataLinkProvenance;
}

export interface DataLinkCatalogMappedColumn {
  table: string | null;
  name: string;
}

export interface DataLinkCatalogItem {
  node: DataLinkBrowserNode;
  provenance: DataLinkProvenance;
  mapping_count: number;
  relation_count: number;
  manual_created?: boolean;
  can_reset_node?: boolean;
  can_reset_mapping?: boolean;
  automatic?: DataLinkAutomaticSemantics | null;
  primary_mapping?: DataLinkCatalogPrimaryMapping | null;
  mapped_columns?: DataLinkCatalogMappedColumn[];
}

export interface DataLinkCatalog {
  datasource_id: string;
  graph_version: string;
  items: DataLinkCatalogItem[];
  page: number;
  page_size: number;
  total: number;
}

export interface DataLinkCatalogMapping {
  field_to_concept_provenance?: DataLinkProvenance;
  entity_to_concept_provenance?: DataLinkProvenance;
  enabled?: boolean;
  column: DataLinkBrowserNode;
  concept: DataLinkBrowserNode;
  entity: DataLinkBrowserNode | null;
  field_to_concept_confidence: number | null;
  entity_to_concept_confidence: number | null;
}

export interface DataLinkCatalogDetail {
  datasource_id: string;
  graph_version: string;
  item: DataLinkCatalogItem;
  mappings: DataLinkCatalogMapping[];
  page: number;
  page_size: number;
  total: number;
}

export interface DataLinkCatalogRelation {
  enabled: boolean;
  id: string;
  source: DataLinkBrowserNode;
  target: DataLinkBrowserNode;
  type: DataLinkEdgeType;
  confidence: number | null;
  evidence: { kind: string; summary: string } | null;
  provenance: DataLinkProvenance;
  join_eligible: boolean;
}

export interface DataLinkRelations {
  datasource_id: string;
  graph_version: string;
  items: DataLinkCatalogRelation[];
  page: number;
  page_size: number;
  total: number;
}

export type DataLinkChangeType = "disable_relation" | "enable_relation" | "add_relation" | "repoint_relation" | "update_node" | "add_node" | "replace_mapping" | "reset_node" | "reset_mapping" | "reset_relation";
export interface DataLinkDraftChange {
  change_type: DataLinkChangeType;
  object_key: string;
  source_id?: string | null;
  target_id?: string | null;
  relation_type?: "joinable" | null;
  enabled?: boolean | null;
  name?: string | null;
  description?: string | null;
  aliases?: string[] | null;
  semantic_type?: string | null;
  reason?: string | null;
  node_type?: "concept" | "entity" | null;
  target_ids?: string[] | null;
}
export interface DataLinkDraft {
  datasource_id: string;
  base_graph_version: string;
  schema_revision: number;
  draft_revision: number;
  status: "active" | "published" | "discarded";
  changes: DataLinkDraftChange[];
}
export interface DataLinkPublishResult {
  datasource_id: string;
  graph_version: string;
  previous_graph_version: string;
  draft_revision: number;
  idempotency_key: string;
}

export interface DataLinkDraftPreviewRequest {
  expected_draft_revision: number;
  query: string;
  focus?: "schema" | "data_profile" | "join_paths" | null;
  max_nodes?: number;
}

export interface DataLinkDraftPreview {
  datasource_id: string;
  base_graph_version: string;
  schema_revision: number;
  draft_revision: number;
  semantic_context: DataLinkSemanticContext;
  retrieval_mode: "keyword";
  is_truncated: boolean;
}

export interface DataLinkVersion {
  build_id: string;
  graph_version: string;
  schema_revision: number;
  status: DataLinkBuildStatus;
  origin_kind: "automated" | "manual" | "restore";
  publication_state: "candidate" | "published";
  base_graph_version: string | null;
  source_graph_version: string | null;
  created_at: string;
  finished_at: string | null;
  is_head: boolean;
  conflict_count: number;
}
export interface DataLinkVersions { datasource_id: string; items: DataLinkVersion[]; page: number; page_size: number; total: number }
export type DataLinkVersionDiffKind = "node_added" | "node_removed" | "node_updated" | "relation_added" | "relation_removed" | "relation_updated" | "mapping_replaced";
export interface DataLinkVersionDiffRelation {
  source_id: string;
  source_name: string;
  target_id: string;
  target_name: string;
  type: DataLinkEdgeType;
  enabled: boolean;
  provenance: DataLinkProvenance;
}
export interface DataLinkVersionDiffItem {
  kind: DataLinkVersionDiffKind;
  object_key: string;
  object_kind: "node" | "relation" | "mapping";
  name: string;
  automatic?: DataLinkAutomaticSemantics | null;
  effective?: DataLinkAutomaticSemantics | null;
  before_relation?: DataLinkVersionDiffRelation | null;
  after_relation?: DataLinkVersionDiffRelation | null;
  before_targets: string[];
  after_targets: string[];
}
export interface DataLinkVersionDiff {
  datasource_id: string;
  graph_version: string;
  base_graph_version: string | null;
  origin_kind: "automated" | "manual" | "restore";
  publication_state: "candidate" | "published";
  items: DataLinkVersionDiffItem[];
  truncated: boolean;
}
export interface DataLinkRestoreRequest { target_graph_version: string; expected_head: string; schema_revision: number; idempotency_key: string }
export interface DataLinkVersionPublish { datasource_id: string; graph_version: string; previous_graph_version: string; source_graph_version: string; origin_kind: "manual" | "restore"; idempotency_key: string }
export interface DataLinkRebuildConflict { object_key: string; object_kind: "node" | "relation" | "mapping"; name: string; reason: string; node_type: DataLinkNodeType | null; edge_type: DataLinkEdgeType | null }
export interface DataLinkRebuildConflicts { datasource_id: string; candidate_graph_version: string; base_graph_version: string; schema_revision: number; items: DataLinkRebuildConflict[] }
export interface DataLinkConflictResolution { object_key: string; action: "discard" | "rebind"; node_id?: string | null; source_id?: string | null; target_id?: string | null }
export interface DataLinkResolveCandidateRequest { candidate_graph_version: string; expected_head: string; schema_revision: number; idempotency_key: string; resolutions: DataLinkConflictResolution[] }

export type DataLinkValidationStatus = "running" | "completed" | "partial" | "failed" | "canceled" | "interrupted";
export interface DataLinkValidation {
  id: string;
  datasource_id: string;
  relation_id: string;
  graph_version: string;
  schema_revision: number;
  status: DataLinkValidationStatus;
  source_non_null_count: number | null;
  target_non_null_count: number | null;
  source_distinct_count: number | null;
  target_distinct_count: number | null;
  target_duplicate_count: number | null;
  source_unmatched_count: number | null;
  multiple_match_risk: boolean | null;
  direction: "source_to_target" | "target_to_source";
  endpoint_fingerprint: string;
  audit_log_ids: string[];
  artifact_ids: string[];
  error_code: string | null;
  expired: boolean;
  created_at: string;
  finished_at: string | null;
}
export interface DataLinkValidationRequest {
  relation_id: string;
  graph_version: string;
  schema_revision: number;
  idempotency_key: string;
}

export type DataLinkConsumptionStage = "prepare" | "tool";
export type DataLinkConsumptionPayloadStatus = "complete" | "too_large" | "unavailable";
export type DataLinkConsumptionReturnedStatus = "ok" | "no_match" | "truncated" | "unavailable" | "too_large";
export type DataLinkConsumptionReceiptStatus = "received" | "ignored_empty" | "not_received";
export interface DataLinkConsumptionSummary {
  field_count: number;
  relationship_count: number;
  join_path_count: number;
  warning_count: number;
  payload_bytes: number;
}
export interface DataLinkConsumption {
  id: string;
  run_id: string;
  stage: DataLinkConsumptionStage;
  seq: number;
  query: string;
  focus: "schema" | "data_profile" | "join_paths" | null;
  max_nodes: number;
  schema_revision: number;
  graph_version: string | null;
  mode: "live" | "cached" | null;
  payload_status: DataLinkConsumptionPayloadStatus;
  returned_status: DataLinkConsumptionReturnedStatus;
  consumer_receipt_status: DataLinkConsumptionReceiptStatus;
  is_truncated: boolean;
  tool_call_id: string | null;
  payload_version: number;
  semantic_context: DataLinkSemanticContext | null;
  summary: DataLinkConsumptionSummary;
  created_at: string;
}
export interface DataLinkConsumptionList {
  run_id: string;
  historical_status: "recorded" | "missing";
  items: DataLinkConsumption[];
}

export interface DataLinkSemanticConcept {
  provenance?: DataLinkProvenance;
  name: string;
  description: string | null;
  aliases: string[];
}

export interface DataLinkSemanticField {
  provenance?: DataLinkProvenance;
  table: string;
  column: string;
  description: string | null;
  semantic_type: string | null;
  aliases: string[];
  semantic_mappings: {
    provenance?: DataLinkProvenance;
    concept: DataLinkSemanticConcept;
    field_to_concept_confidence: number | null;
    entities: {
      provenance?: DataLinkProvenance;
      entity: DataLinkSemanticConcept;
      entity_to_concept_confidence: number | null;
    }[];
  }[];
}

export interface DataLinkSemanticRelationship {
  provenance?: DataLinkProvenance;
  source_table: string;
  source_column: string;
  target_table: string;
  target_column: string;
  edge_type: "foreign_key" | "joinable";
  confidence: number | null;
  evidence: { kind: string; summary: string } | null;
}

export interface DataLinkSemanticContext {
  provider: "datalink";
  trust: "inferred";
  mode: "live" | "cached";
  graph_version: string;
  semantic_catalog: { concepts: DataLinkSemanticConcept[]; entities: DataLinkSemanticConcept[] };
  fields: DataLinkSemanticField[];
  relationships: DataLinkSemanticRelationship[];
  join_paths: {
    provenance?: DataLinkProvenance;
    tables: string[];
    steps: DataLinkSemanticRelationship[];
    confidence: number | null;
    evidence: string | null;
  }[];
  warnings: string[];
}

export interface DataLinkPreviewRequest {
  graph_version: string;
  query: string;
  focus?: "schema" | "data_profile" | "join_paths" | null;
  max_nodes?: number;
}

export interface DataLinkPreview {
  datasource_id: string;
  schema_revision: number;
  graph_version: string;
  query: string;
  focus: "schema" | "data_profile" | "join_paths" | null;
  max_nodes: number;
  semantic_context: DataLinkSemanticContext;
  retrieval_mode: "keyword" | "hybrid" | "unknown";
  is_truncated: boolean;
}

export interface DataLinkStatus {
  datasource_id: string;
  current_graph_version: string | null;
  current_build: DataLinkBuild | null;
  last_error_code: string | null;
  last_error_message: string | null;
}

export interface DataLinkRebuildResult {
  publication_state: "candidate" | "published";
  build_id: string;
  datasource_id: string;
  status: DataLinkBuildStatus;
  requested_schema_revision: number;
  graph_version: string | null;
}

export interface ModelProfile {
  id: string;
  name: string;
  provider: string;
  model_name: string;
  base_url: string;
  temperature: number;
  run_timeout_seconds: number;
  context_window_tokens: number | null;
  status: ModelProfileStatus;
  is_active: boolean;
  has_api_key: boolean;
  tool_calling_supported: boolean | null;
  final_output_mode: FinalOutputMode | null;
  capability_contract_version: string | null;
  created_at: string;
  updated_at: string;
}

export interface ModelProfileCreate {
  name: string;
  provider?: string;
  model_name: string;
  base_url: string;
  api_key: string;
  temperature?: number;
  run_timeout_seconds?: number;
  context_window_tokens?: number | null;
}

export interface ModelProfileUpdate {
  name?: string | null;
  provider?: string | null;
  model_name?: string | null;
  base_url?: string | null;
  api_key?: string | null;
  temperature?: number | null;
  run_timeout_seconds?: number | null;
  context_window_tokens?: number | null;
}

export interface ModelProfileTestResult {
  profile_id: string;
  status: ModelProfileStatus;
  tool_calling_supported: boolean;
  final_output_mode: FinalOutputMode | null;
  capability_contract_version: string | null;
  message: string;
}

export const RUN_EVENT_TYPES = [
  "run.queued",
  "run.started",
  "run.preparation.started",
  "run.preparation.completed",
  "run.protocol.selected",
  "analysis.clarification.requested",
  "analysis.requirement.blocked",
  "analysis.discovery.observed",
  "agent.turn.started",
  "agent.turn.completed",
  "final_answer.request.started",
  "final_answer.response.received",
  "final_answer.validation.failed",
  "final_answer.request.timed_out",
  "tool.called",
  "tool.succeeded",
  "tool.failed",
  "artifact.created",
  "answer.delta",
  "answer.ready",
  "run.cancel_requested",
  "run.succeeded",
  "run.failed",
  "run.canceled",
] as const;

export type RunEventType = (typeof RUN_EVENT_TYPES)[number];
export type RunEventPayload = Record<string, Scalar>;

export interface RunEvent {
  run_id: string;
  seq: number;
  type: RunEventType;
  timestamp: string;
  payload: RunEventPayload;
}
