import type {
  CompletionKind,
  Run,
  RunArtifact,
  RunEvent,
  RunStatus,
  SqlAudit,
  ToolCall,
} from "@/api/types";

export type RunActivityItemKind = "analysis" | "tool" | "artifact" | "answer";
export type RunActivityItemStatus = "waiting" | "running" | "succeeded" | "failed" | "canceled";
export type RunActivityTarget = "overview" | "trace" | "outputs" | "details";

export interface RunActivityItem {
  id: string;
  runId: string;
  seq: number;
  kind: RunActivityItemKind;
  status: RunActivityItemStatus;
  title: string;
  detail: string | null;
  target: RunActivityTarget;
  toolCallId: string | null;
  artifactId: string | null;
}

export interface RunActivityStep {
  id: string;
  runId: string;
  seq: number;
  title: string;
  detail: string | null;
  goal: string;
  action: string;
  result: string | null;
  status: RunActivityItemStatus;
  turnNo: number | null;
  actionKind?: string | null;
  toolNames?: string[];
  elapsedMs?: number | null;
  failureCode?: string | null;
  toolCallIds: string[];
  items: RunActivityItem[];
}

export type RunActivityDisplayEntry =
  | {
      kind: "step";
      id: string;
      seq: number;
      stepNumber: number;
      step: RunActivityStep;
    }
  | {
      kind: "item";
      id: string;
      seq: number;
      item: RunActivityItem;
    };

export interface RunActivity {
  runId: string;
  status: RunStatus | null;
  completionKind: CompletionKind | null;
  summary: string;
  items: RunActivityItem[];
  steps: RunActivityStep[];
  displayEntries: RunActivityDisplayEntry[];
  toolCount: number;
  artifactCount: number;
  auditCount: number;
}

export interface RunActivityFacts {
  run: Run | null;
  events: readonly RunEvent[];
  toolCalls: readonly ToolCall[];
  sqlAudits: readonly SqlAudit[];
  artifacts: readonly RunArtifact[];
}

interface MutableToolActivity {
  item: RunActivityItem;
  eventStatus: RunActivityItemStatus;
  elapsedMs: number | null;
  turnNo: number;
}

interface MutablePreparationActivity {
  item: RunActivityItem;
  completed: boolean;
  elapsedMs: number | null;
  failureCode: string | null;
}

interface MutableAgentTurnActivity {
  turnNo: number;
  seq: number;
  status: RunActivityItemStatus;
  elapsedMs: number | null;
  actionKind: string | null;
  toolNames: string[];
  toolCallCount: number | null;
  failureCode: string | null;
}

function payloadString(event: RunEvent, key: string): string | null {
  const value = event.payload[key];
  return typeof value === "string" && value.length > 0 ? value : null;
}

function payloadNumber(event: RunEvent, key: string): number | null {
  const value = event.payload[key];
  return typeof value === "number" && Number.isFinite(value) && value >= 0 ? value : null;
}

function payloadCompletionKind(event: RunEvent): CompletionKind | null {
  const value = payloadString(event, "completion_kind");
  return value === "completed" || value === "partial" || value === "clarification" ? value : null;
}

function answerActivityCopy(completionKind: CompletionKind | null): { title: string; detail: string } {
  if (completionKind === "clarification") {
    return { title: "已请求补充分析范围", detail: "等待补充后继续分析" };
  }
  return { title: "正式答案已生成", detail: "可在对话中阅读" };
}

function preparationLabel(phase: string | null): string | null {
  switch (phase) {
    case "run_opening":
      return "判断本次处理方式";
    case "semantic_context":
      return "读取数据地图";
    case "analysis_plan":
      return "校验分析计划";
    default:
      return null;
  }
}

function finalAnswerAttemptKey(runId: string, event: RunEvent): string {
  const attempt = payloadNumber(event, "attempt");
  return `final-answer:${runId}:${attempt ?? "none"}`;
}

function finalAnswerTerminalStatus(status: RunStatus | null | undefined): RunActivityItemStatus | null {
  if (status === "canceled") return "canceled";
  if (status === "failed") return "failed";
  if (status === "succeeded") return "succeeded";
  return null;
}

function applyFinalAnswerEvent(item: RunActivityItem, event: RunEvent): void {
  const attempt = payloadNumber(event, "attempt");
  const attemptLabel = attempt === null ? "" : `第 ${attempt} 次`;
  const elapsedMs = payloadNumber(event, "elapsed_ms");
  switch (event.type) {
    case "final_answer.request.started":
      item.status = "running";
      item.title = "开始生成最终答案";
      item.detail = `${attemptLabel}请求已发出`.trim();
      return;
    case "final_answer.response.received":
      item.status = "succeeded";
      item.title = "收到最终答案模型回复";
      item.detail = elapsedMs === null ? "回复已收到" : `回复已收到，用时 ${elapsedMs} ms`;
      return;
    case "final_answer.validation.failed":
      item.status = "failed";
      item.title = "最终答案未通过校验";
      item.detail = payloadString(event, "validation_stage") === "markdown"
        ? "结构化答案已收到，但 Markdown 块结构不合规，原文不会展示"
        : "结构化答案未通过校验，原文不会展示";
      return;
    case "final_answer.request.timed_out":
      item.status = "failed";
      item.title = "最终答案生成超时";
      item.detail = elapsedMs === null ? "请求超过阶段时限" : `请求在 ${elapsedMs} ms 后超过阶段时限`;
      return;
    default:
      return;
  }
}

export function toolLabel(name: string | null): string {
  switch (name) {
    case "run_sql_readonly":
      return "查询数据";
    case "run_python":
      return "运行 Python 分析";
    case "explore_datalink":
      return "查看数据关系";
    case "commit_analysis_claims":
      return "提交分析结论";
    default:
      return "执行分析工具";
  }
}

function inputString(tool: ToolCall | undefined, key: string): string | null {
  const value = tool?.input_params?.[key];
  return typeof value === "string" && value.trim().length > 0 ? value.trim() : null;
}

function summaryNumber(tool: ToolCall | undefined, key: string): number | null {
  const value = tool?.output_summary?.[key];
  return typeof value === "number" && Number.isFinite(value) && value >= 0 ? value : null;
}

function formatCount(value: number): string {
  return value.toLocaleString("zh-CN");
}

function uniqueText(values: readonly (string | null)[]): string[] {
  return [...new Set(values.filter((value): value is string => value !== null && value.length > 0))];
}

function latestSuccessfulAudit(audits: readonly SqlAudit[]): SqlAudit | null {
  const successful = audits
    .filter((audit) => audit.status === "succeeded")
    .sort((left, right) => left.attempt_no - right.attempt_no);
  return successful[successful.length - 1] ?? null;
}

function latestAudit(audits: readonly SqlAudit[]): SqlAudit | null {
  return [...audits].sort((left, right) => left.attempt_no - right.attempt_no).at(-1) ?? null;
}

function toolGoal(tool: ToolCall | undefined, audits: readonly SqlAudit[]): string {
  if (tool?.tool_name === "run_python") {
    return inputString(tool, "purpose") ?? "完成 Python 分析";
  }
  if (tool?.tool_name === "explore_datalink") {
    const focus = inputString(tool, "focus");
    if (focus === "schema") return "确认数据结构";
    if (focus === "data_profile") return "确认数据概况";
    if (focus === "join_paths") return "确认数据表之间的关联";
    return inputString(tool, "query") ?? "确认数据关系";
  }
  if (tool?.tool_name === "run_sql_readonly") {
    const audit = latestSuccessfulAudit(audits);
    const tables = audit?.referenced_tables ?? [];
    return tables.length > 0 ? `确认 ${tables.join("、")} 的查询结果` : "确认查询结果";
  }
  return "确认分析所需数据";
}

export function toolResultSummary(
  item: RunActivityItem,
  tool: ToolCall | undefined,
  audits: readonly SqlAudit[],
  artifacts: readonly RunArtifact[],
): string | null {
  if (item.status === "running") return "正在等待结果";
  if (item.status === "failed") {
    const error = tool?.error_message ?? tool?.error_code;
    const audit = latestAudit(audits);
    return error === null || error === undefined
      ? audit?.blocked_reason ?? "调用未完成"
      : `调用未完成：${error}`;
  }
  if (item.status === "canceled") return "已取消";
  if (item.status !== "succeeded") return null;

  if (tool?.tool_name === "run_sql_readonly") {
    const audit = latestSuccessfulAudit(audits) ?? latestAudit(audits);
    const parts = [
      ...(audit?.row_count === null || audit?.row_count === undefined
        ? []
        : [`返回 ${formatCount(audit.row_count)} 行`]),
      ...(audit?.referenced_tables.length ? [`涉及 ${audit.referenced_tables.join("、")}`] : []),
      ...(audit?.elapsed_ms === null || audit?.elapsed_ms === undefined ? [] : [`耗时 ${audit.elapsed_ms} ms`]),
      ...(audit?.status === "succeeded" ? ["审计通过"] : []),
      ...(audit?.blocked_reason ? [audit.blocked_reason] : []),
    ];
    return parts.join("；") || "查询完成";
  }

  if (tool?.tool_name === "explore_datalink") {
    const nodeCount = summaryNumber(tool, "node_count");
    const edgeCount = summaryNumber(tool, "edge_count");
    const semanticEntityCount = summaryNumber(tool, "semantic_entity_count");
    const semanticMappingCount = summaryNumber(tool, "semantic_mapping_count");
    if (nodeCount !== null && semanticEntityCount !== null && semanticEntityCount > 0) {
      const mappings = semanticMappingCount !== null && semanticMappingCount > 0
        ? `、${formatCount(semanticMappingCount)} 条字段实体关联`
        : "";
      const physicalRelations = edgeCount !== null && edgeCount > 0
        ? `、${formatCount(edgeCount)} 条物理关系`
        : "";
      return `找到 ${formatCount(nodeCount)} 个字段，关联 ${formatCount(semanticEntityCount)} 个语义实体${mappings}${physicalRelations}`;
    }
    if (nodeCount !== null && edgeCount !== null) {
      return `找到 ${formatCount(nodeCount)} 个节点、${formatCount(edgeCount)} 条关系`;
    }
    if (nodeCount !== null) return `找到 ${formatCount(nodeCount)} 个节点`;
    if (edgeCount !== null) return `找到 ${formatCount(edgeCount)} 条关系`;
    return "关系查看完成";
  }

  if (tool?.tool_name === "run_python") {
    const exitCode = summaryNumber(tool, "exit_code");
    const outputCount = summaryNumber(tool, "output_count");
    const parts = [
      ...(exitCode === null ? [] : [`退出码 ${exitCode}`]),
      ...(artifacts.length > 0
        ? [`生成 ${formatCount(artifacts.length)} 个产物`]
        : outputCount === null ? [] : [`登记 ${formatCount(outputCount)} 个产物`]),
    ];
    return parts.join("；") || (exitCode === 0 ? "分析脚本运行成功" : "Python 分析完成");
  }

  if (tool?.tool_name === "commit_analysis_claims") {
    const claimCount = summaryNumber(tool, "claim_count");
    const evidenceCount = summaryNumber(tool, "evidence_binding_count");
    if (claimCount !== null && evidenceCount !== null) {
      return `提交 ${formatCount(claimCount)} 个结论，绑定 ${formatCount(evidenceCount)} 条事实证据`;
    }
    if (claimCount !== null) return `提交 ${formatCount(claimCount)} 个分析结论`;
    return "分析结论已提交";
  }

  if (artifacts.length > 0) return `生成 ${formatCount(artifacts.length)} 个产物`;
  return "已完成";
}

function artifactLabel(type: string | null): string {
  switch (type) {
    case "table":
      return "已生成数据表";
    case "chart":
      return "已生成图表";
    case "markdown":
      return "已生成分析说明";
    case "file":
      return "已生成文件";
    default:
      return "已生成产物";
  }
}

function itemStatusFromTool(tool: ToolCall | undefined, eventStatus: RunActivityItemStatus): RunActivityItemStatus {
  if (tool?.status === "succeeded") return "succeeded";
  if (tool?.status === "failed") return "failed";
  return eventStatus;
}

function toolDetail(status: RunActivityItemStatus, elapsedMs: number | null): string | null {
  if (status === "running") return "正在执行";
  if (status === "failed") return "未完成";
  if (status === "succeeded" && elapsedMs !== null) return `完成，用时 ${elapsedMs} ms`;
  if (status === "succeeded") return "完成";
  return null;
}

function agentTurnStatus(value: string | null): RunActivityItemStatus {
  if (value === "failed") return "failed";
  if (value === "cancelled") return "canceled";
  if (value === "completed") return "succeeded";
  return "running";
}

function agentTurnAction(turn: MutableAgentTurnActivity): string {
  if (turn.actionKind === "model_error") return "模型调用失败";
  if (turn.actionKind === "respond") return "直接生成回复";
  if (turn.toolNames.length > 0) {
    return `调用${turn.toolNames.map(toolLabel).join("、")}`;
  }
  if (turn.toolCallCount === 0) return "未调用工具";
  return "决定下一步动作";
}

function agentTurnDetail(
  turn: MutableAgentTurnActivity,
  status: RunActivityItemStatus,
  itemCount: number,
  elapsedMs: number | null,
): string {
  if (status === "running") return `第 ${turn.turnNo} 轮 Agent 正在决定下一步`;
  if (status === "failed") {
    return turn.failureCode === null ? "Agent 回合未完成" : `Agent 回合未完成：${turn.failureCode}`;
  }
  if (status === "canceled") return `第 ${turn.turnNo} 轮 Agent 已取消`;
  const elapsed = elapsedMs === null ? "已完成" : `完成，用时 ${elapsedMs} ms`;
  return itemCount > 0 ? `已决定 ${itemCount} 个工具动作；${elapsed}` : elapsed;
}

function stepStatus(items: readonly RunActivityItem[]): RunActivityItemStatus {
  if (items.some((item) => item.status === "failed")) return "failed";
  if (items.some((item) => item.status === "canceled")) return "canceled";
  if (items.some((item) => item.status === "running")) return "running";
  if (items.length > 0 && items.every((item) => item.status === "succeeded")) return "succeeded";
  return "waiting";
}

function stepDetail(items: readonly RunActivityItem[], elapsedMs: number): string | null {
  const status = stepStatus(items);
  if (status === "running") return `正在执行 ${items.length} 个工具`;
  if (status === "failed") return `${items.length} 个工具中有调用未完成`;
  if (status === "canceled") return "本步骤已取消";
  if (status === "succeeded") {
    return elapsedMs > 0 ? `完成 ${items.length} 个工具，用时 ${elapsedMs} ms` : `完成 ${items.length} 个工具`;
  }
  return null;
}

function preparationDetail(
  status: RunActivityItemStatus,
  elapsedMs: number | null,
  failureCode: string | null,
): string {
  if (status === "succeeded") {
    const elapsed = elapsedMs === null ? "已完成" : `已完成，用时 ${elapsedMs} ms`;
    return `${elapsed}；暂无可展示的结构化结果`;
  }
  if (status === "failed") return failureCode === null ? "未完成" : `未完成：${failureCode}`;
  if (status === "canceled") return failureCode === null ? "已取消" : `已取消：${failureCode}`;
  return "正在处理";
}

function stepTitle(items: readonly RunActivityItem[], index: number): string {
  const labels = [...new Set(items.map((item) => item.title))];
  return `第 ${index + 1} 步 · ${labels.join("、") || "执行分析"}`;
}

function inferredStatus(events: readonly RunEvent[], runId: string): RunStatus | null {
  for (const event of [...events].reverse()) {
    if (event.run_id !== runId) continue;
    if (event.type === "run.succeeded") return "succeeded";
    if (event.type === "run.failed") return "failed";
    if (event.type === "run.canceled") return "canceled";
    if (event.type === "run.started") return "running";
    if (event.type === "run.queued") return "queued";
  }
  return null;
}

function summaryFor(
  status: RunStatus | null,
  completionKind: CompletionKind | null,
  items: readonly RunActivityItem[],
): string {
  if (status === "succeeded") {
    if (completionKind === "partial") return "分析部分完成";
    if (completionKind === "clarification") return "等待补充分析范围";
    return "分析完成";
  }
  if (status === "failed") return "分析未完成";
  if (status === "canceled") return "分析已取消";

  const runningTool = [...items].reverse().find((item) => item.kind === "tool" && item.status === "running");
  if (runningTool) return `正在${runningTool.title}`;
  const runningAnalysis = [...items].reverse().find((item) => item.kind === "analysis" && item.status === "running");
  if (runningAnalysis) return `正在${runningAnalysis.title}`;
  if (items.some((item) => item.kind === "answer" && item.status === "running")) return "正在整理答案";
  if (status === "queued") return "正在准备分析";
  return "正在分析";
}

/**
 * 将同一 Run 的持久化事实压缩为可展示的活动。这里是唯一读取事件 payload 的前端边界，
 * 关联始终使用 run_id、seq、tool_call_id 和 artifact_id，不生成第二套游标。
 */
export function deriveRunActivity({ run, events, toolCalls, sqlAudits, artifacts }: RunActivityFacts): RunActivity {
  const runId = run?.id ?? events[0]?.run_id ?? "";
  const runEvents = events.filter((event) => event.run_id === runId).sort((left, right) => left.seq - right.seq);
  const toolsById = new Map(toolCalls.filter((tool) => tool.run_id === runId).map((tool) => [tool.id, tool]));
  const artifactsById = new Map(
    artifacts.filter((artifact) => artifact.run_id === runId).map((artifact) => [artifact.id, artifact]),
  );
  const toolActivities = new Map<string, MutableToolActivity>();
  const preparationActivities = new Map<string, MutablePreparationActivity>();
  const agentTurns = new Map<number, MutableAgentTurnActivity>();
  const finalAnswerActivities = new Map<string, RunActivityItem>();
  const items: RunActivityItem[] = [];
  const toolOrder: string[] = [];
  let firstAnalysisSeq: number | null = null;
  let answerSeq: number | null = null;
  let answerReady = false;
  let answerCompletionKind: CompletionKind | null = run?.completion_kind ?? null;

  for (const event of runEvents) {
    if (
      event.type === "final_answer.request.started" ||
      event.type === "final_answer.response.received" ||
      event.type === "final_answer.validation.failed" ||
      event.type === "final_answer.request.timed_out"
    ) {
      const key = finalAnswerAttemptKey(runId, event);
      const existing = finalAnswerActivities.get(key);
      if (existing === undefined) {
        const item: RunActivityItem = {
          id: key,
          runId,
          seq: event.seq,
          kind: "analysis",
          status: "running",
          title: "开始生成最终答案",
          detail: null,
          target: "overview",
          toolCallId: null,
          artifactId: null,
        };
        applyFinalAnswerEvent(item, event);
        finalAnswerActivities.set(key, item);
      } else {
        applyFinalAnswerEvent(existing, event);
      }
      continue;
    }

    if (event.type === "run.preparation.started" || event.type === "run.preparation.completed") {
      const phase = payloadString(event, "phase");
      const title = preparationLabel(phase);
      if (phase === null || title === null) continue;
      const existing = preparationActivities.get(phase);
      if (event.type === "run.preparation.started") {
        preparationActivities.set(phase, existing ?? {
          item: {
            id: `preparation:${phase}`,
            runId,
            seq: event.seq,
            kind: "analysis",
            status: "running",
            title,
            detail: "正在处理",
            target: "overview",
            toolCallId: null,
            artifactId: null,
          },
          completed: false,
          elapsedMs: null,
          failureCode: null,
        });
      } else if (existing !== undefined) {
        const preparationStatus = payloadString(event, "status");
        existing.completed = preparationStatus === null || preparationStatus === "completed";
        existing.elapsedMs = payloadNumber(event, "elapsed_ms");
        existing.failureCode = payloadString(event, "failure_code");
        if (preparationStatus !== null && preparationStatus !== "completed") {
          existing.item.status = preparationStatus === "cancelled" ? "canceled" : "failed";
          existing.item.detail = payloadString(event, "failure_code") ?? "准备阶段未完成";
        } else {
          existing.item.status = "succeeded";
        }
      } else {
        const preparationStatus = payloadString(event, "status");
        const failed = preparationStatus !== null && preparationStatus !== "completed";
        preparationActivities.set(phase, {
          item: {
            id: `preparation:${phase}`,
            runId,
            seq: event.seq,
            kind: "analysis",
            status: failed ? preparationStatus === "cancelled" ? "canceled" : "failed" : "succeeded",
            title,
            detail: failed ? payloadString(event, "failure_code") ?? "准备阶段未完成" : null,
            target: "overview",
            toolCallId: null,
            artifactId: null,
          },
          completed: !failed,
          elapsedMs: payloadNumber(event, "elapsed_ms"),
          failureCode: payloadString(event, "failure_code"),
        });
      }
      continue;
    }

    if (event.type === "agent.turn.started" || event.type === "agent.turn.completed") {
      firstAnalysisSeq ??= event.seq;
      if (event.type === "agent.turn.started") {
        const turnNo = payloadNumber(event, "turn_no");
        if (turnNo === null) continue;
        const existing = agentTurns.get(turnNo);
        agentTurns.set(turnNo, existing ?? {
          turnNo,
          seq: event.seq,
          status: "running",
          elapsedMs: null,
          actionKind: null,
          toolNames: [],
          toolCallCount: null,
          failureCode: null,
        });
      } else {
        const turnNo = payloadNumber(event, "turn_no");
        if (turnNo !== null) {
          const existing = agentTurns.get(turnNo) ?? {
            turnNo,
            seq: event.seq,
            status: "running" as const,
            elapsedMs: null,
            actionKind: null,
            toolNames: [],
            toolCallCount: null,
            failureCode: null,
          };
          existing.status = agentTurnStatus(payloadString(event, "status") ?? "completed");
          existing.elapsedMs = payloadNumber(event, "elapsed_ms");
          existing.actionKind = payloadString(event, "action_kind");
          existing.toolNames = (payloadString(event, "tool_names") ?? "")
            .split(",")
            .filter((name) => name.length > 0);
          existing.toolCallCount = payloadNumber(event, "tool_call_count");
          existing.failureCode = payloadString(event, "failure_code");
          agentTurns.set(turnNo, existing);
        }
      }
      continue;
    }

    if (event.type === "run.queued" || event.type === "run.started" || event.type === "run.cancel_requested") {
      firstAnalysisSeq ??= event.seq;
      continue;
    }

    if (event.type === "tool.called" || event.type === "tool.succeeded" || event.type === "tool.failed") {
      const toolCallId = payloadString(event, "tool_call_id");
      const turnNo = payloadNumber(event, "turn_no");
      const toolName = payloadString(event, "tool_name");
      if (toolCallId === null || turnNo === null || toolName === null) continue;
      const eventStatus: RunActivityItemStatus = event.type === "tool.called"
        ? "running"
        : event.type === "tool.succeeded"
          ? "succeeded"
          : "failed";
      const existing = toolActivities.get(toolCallId);
      if (existing === undefined) {
        const item: RunActivityItem = {
          id: `tool:${toolCallId}`,
          runId,
          seq: event.seq,
          kind: "tool",
          status: eventStatus,
          title: toolLabel(toolName),
          detail: null,
          target: "trace",
          toolCallId,
          artifactId: null,
        };
        toolActivities.set(toolCallId, {
          item,
          eventStatus,
          elapsedMs: null,
          turnNo,
        });
        toolOrder.push(toolCallId);
      } else {
        existing.eventStatus = eventStatus;
      }
      const activity = toolActivities.get(toolCallId);
      if (activity !== undefined) {
        activity.elapsedMs = payloadNumber(event, "elapsed_ms") ?? activity.elapsedMs;
      }
      continue;
    }

    if (event.type === "artifact.created") {
      const artifactId = payloadString(event, "artifact_id");
      if (artifactId === null) continue;
      const artifact = artifactsById.get(artifactId);
      items.push({
        id: `artifact:${artifactId}`,
        runId,
        seq: event.seq,
        kind: "artifact",
        status: "succeeded",
        title: artifact?.title ?? artifactLabel(payloadString(event, "artifact_type")),
        detail: "可查看",
        target: "outputs",
        toolCallId: null,
        artifactId: artifact?.id ?? null,
      });
      continue;
    }

    if (event.type === "answer.delta") {
      answerSeq ??= event.seq;
      continue;
    }

    if (event.type === "answer.ready") {
      answerSeq ??= event.seq;
      answerReady = true;
      answerCompletionKind = payloadCompletionKind(event) ?? answerCompletionKind;
    }
  }

  const finalAnswerStatus = finalAnswerTerminalStatus(run?.status);
  for (const item of finalAnswerActivities.values()) {
    items.push(
      item.status === "running" && finalAnswerStatus !== null
        ? { ...item, status: finalAnswerStatus }
        : item,
    );
  }

  if (preparationActivities.size > 0) {
    for (const { item, completed, elapsedMs, failureCode } of preparationActivities.values()) {
      // A preparation failure/cancellation is a fact about that phase. Keep it
      // visible even when the Run continues through a schema-only fallback and
      // later finishes with a succeeded/partial terminal status.
      const status: RunActivityItemStatus = completed
        ? "succeeded"
        : item.status === "failed" || item.status === "canceled"
          ? item.status
          : run?.status === "failed"
            ? "failed"
            : run?.status === "canceled"
              ? "canceled"
              : "running";
      const detail = preparationDetail(status, elapsedMs, failureCode);
      items.push({ ...item, status, detail });
    }
  } else if (firstAnalysisSeq !== null && agentTurns.size === 0) {
    items.push({
      id: `analysis:${runId}`,
      runId,
      seq: firstAnalysisSeq,
      kind: "analysis",
      status: run?.status === "canceled" ? "canceled" : run?.status === "failed" ? "failed" : "succeeded",
      title: "理解分析问题",
      detail: run?.status === "failed" ? "未完成" : run?.status === "canceled" ? "已取消" : "已准备好",
      target: "overview",
      toolCallId: null,
      artifactId: null,
    });
  }

  for (const { item, eventStatus, elapsedMs } of toolActivities.values()) {
    const status = itemStatusFromTool(toolsById.get(item.toolCallId ?? ""), eventStatus);
    items.push({ ...item, status, detail: toolDetail(status, elapsedMs) });
  }

  const activityItemsByToolId = new Map<string, RunActivityItem>();
  for (const item of items) {
    if (item.kind === "tool" && item.toolCallId !== null) {
      activityItemsByToolId.set(item.toolCallId, item);
    }
  }
  const groupedSteps = new Map<string, { turnNo: number; itemIds: string[]; seq: number; turn: MutableAgentTurnActivity | null }>();
  for (const toolCallId of toolOrder) {
    const activity = toolActivities.get(toolCallId);
    const item = activityItemsByToolId.get(toolCallId);
    if (activity === undefined || item === undefined) continue;
    const key = `turn:${activity.turnNo}`;
    const existing = groupedSteps.get(key);
    if (existing === undefined) {
      groupedSteps.set(key, { turnNo: activity.turnNo, itemIds: [item.id], seq: item.seq, turn: null });
    } else {
      existing.itemIds.push(item.id);
      existing.seq = Math.min(existing.seq, item.seq);
    }
  }
  const stepGroups = agentTurns.size > 0
    ? [...agentTurns.values()].map((turn) => ({
      turnNo: turn.turnNo,
      itemIds: toolOrder
        .filter((toolCallId) => toolActivities.get(toolCallId)?.turnNo === turn.turnNo)
        .map((toolCallId) => activityItemsByToolId.get(toolCallId)?.id)
        .filter((id): id is string => id !== undefined),
      seq: turn.seq,
      turn,
    }))
    : [...groupedSteps.values()];
  const steps = stepGroups
    .sort((left, right) => left.seq - right.seq)
    .map((group, index): RunActivityStep => {
      const stepItems = group.itemIds
        .map((itemId) => items.find((item) => item.id === itemId))
        .filter((item): item is RunActivityItem => item !== undefined);
      const stepFacts = stepItems.map((item) => {
        const tool = item.toolCallId === null ? undefined : toolsById.get(item.toolCallId);
        const audits = sqlAudits.filter((audit) => audit.tool_call_id === item.toolCallId);
        const itemArtifacts = artifacts.filter((artifact) => artifact.tool_call_id === item.toolCallId);
        return {
          action: item.title,
          goal: toolGoal(tool, audits),
          result: toolResultSummary(item, tool, audits, itemArtifacts),
        };
      });
      const toolElapsedMs = group.itemIds.reduce((total, itemId) => {
        const toolCallId = items.find((item) => item.id === itemId)?.toolCallId;
        return total + (toolCallId === null || toolCallId === undefined ? 0 : toolActivities.get(toolCallId)?.elapsedMs ?? 0);
      }, 0);
      const elapsedMs = group.turn?.elapsedMs ?? (toolElapsedMs > 0 ? toolElapsedMs : null);
      const actions = uniqueText(stepFacts.map((fact) => fact.action));
      const goals = uniqueText(stepFacts.map((fact) => fact.goal));
      const results = uniqueText(stepFacts.map((fact) => fact.result));
      const status = group.turn === null
        ? stepStatus(stepItems)
        : stepItems.length > 0 && stepStatus(stepItems) !== "succeeded"
          ? stepStatus(stepItems)
          : group.turn.status;
      const action = group.turn === null ? actions.join("、") || "执行分析" : agentTurnAction(group.turn);
      const result = results.join("；") || (
        group.turn?.failureCode === null || group.turn?.failureCode === undefined
          ? group.turn === null ? null : group.turn.status === "succeeded" ? "本轮没有生成可展示的工具结果" : null
          : `回合未完成：${group.turn.failureCode}`
      );
      return {
        id: `step:${runId}:${group.turnNo ?? `batch-${index + 1}`}`,
        runId,
        seq: group.seq,
        title: group.turn === null ? stepTitle(stepItems, index) : `第 ${group.turn.turnNo} 轮 Agent 分析`,
        detail: group.turn === null
          ? stepDetail(stepItems, toolElapsedMs)
          : agentTurnDetail(group.turn, status, stepItems.length, elapsedMs),
        goal: goals.join("；") || (group.turn === null ? "确认分析所需数据" : "基于当前 Run 事实决定下一步"),
        action,
        result,
        status,
        turnNo: group.turnNo,
        actionKind: group.turn?.actionKind ?? null,
        toolNames: group.turn?.toolNames ?? [],
        elapsedMs,
        failureCode: group.turn?.failureCode ?? null,
        toolCallIds: stepItems.flatMap((item) => item.toolCallId === null ? [] : [item.toolCallId]),
        items: stepItems,
      };
    });

  if (answerSeq !== null) {
    const answerCopy = answerActivityCopy(answerCompletionKind);
    items.push({
      id: `answer:${runId}`,
      runId,
      seq: answerSeq,
      kind: "answer",
      status: answerReady ? "succeeded" : "running",
      title: answerReady ? answerCopy.title : "正在整理答案",
      detail: answerReady ? answerCopy.detail : null,
      target: "overview",
      toolCallId: null,
      artifactId: null,
    });
  }

  const status = run?.status ?? inferredStatus(runEvents, runId);
  const completionKind = answerCompletionKind;
  const sortedItems = items.sort((left, right) => left.seq - right.seq);
  const toolItemIds = new Set(steps.flatMap((step) => step.items.map((item) => item.id)));
  const displayEntries: RunActivityDisplayEntry[] = [
    ...steps.map((step, index) => ({
      kind: "step" as const,
      id: step.id,
      seq: step.seq,
      stepNumber: index + 1,
      step,
    })),
    ...sortedItems
      .filter((item) => !toolItemIds.has(item.id))
      .map((item) => ({
        kind: "item" as const,
        id: item.id,
        seq: item.seq,
        item,
      })),
  ].sort((left, right) => left.seq - right.seq);
  return {
    runId,
    status,
    completionKind,
    summary: summaryFor(status, completionKind, sortedItems),
    items: sortedItems,
    steps,
    displayEntries,
    toolCount: toolActivities.size,
    artifactCount: artifacts.filter((artifact) => artifact.run_id === runId).length,
    auditCount: sqlAudits.filter((audit) => audit.run_id === runId).length,
  };
}
