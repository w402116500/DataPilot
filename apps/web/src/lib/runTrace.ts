import type { Run, RunArtifact, RunEvent, RunEventType, SqlAudit, ToolCall } from "@/api/types";

import { deriveRunActivity, toolLabel, toolResultSummary } from "./runActivity";

export type RunTraceStatus = "queued" | "running" | "succeeded" | "failed" | "canceled";

export interface RunTraceEntry {
  id: string;
  detailId: string;
  seq: number;
  eventType: RunEventType;
  timestamp: string;
  title: string;
  summary: string;
  status: RunTraceStatus;
  statusLabel: string;
  toolCallId: string | null;
  artifactId: string | null;
  resultSummary: string | null;
}

interface RunTraceInput {
  run: Run;
  events: readonly RunEvent[];
  toolCalls: readonly ToolCall[];
  sqlAudits: readonly SqlAudit[];
  artifacts: readonly RunArtifact[];
}

interface TraceContext {
  toolName: string | null;
  toolCall: ToolCall | null;
  auditCount: number;
  artifact: RunArtifact | null;
}

function resultSummaryForEvent(
  event: RunEvent,
  activity: ReturnType<typeof deriveRunActivity>,
  artifact: RunArtifact | null,
  toolCallsById: ReadonlyMap<string, ToolCall>,
  sqlAuditsByToolId: ReadonlyMap<string, readonly SqlAudit[]>,
  artifactsByToolId: ReadonlyMap<string, readonly RunArtifact[]>,
): string | null {
  const toolCallId = payloadString(event, "tool_call_id");
  if (toolCallId !== null && event.type === "tool.succeeded") {
    const item = activity.items.find((candidate) => candidate.toolCallId === toolCallId);
    if (item === undefined) return null;
    const toolCall = toolCallsById.get(toolCallId);
    return toolResultSummary(
      item,
      toolCall,
      sqlAuditsByToolId.get(toolCallId) ?? [],
      artifactsByToolId.get(toolCallId) ?? [],
    );
  }

  if (event.type === "run.preparation.completed") {
    const phase = payloadString(event, "phase");
    const detail = phase === null ? null : activity.items.find((item) => item.id === `preparation:${phase}`)?.detail ?? null;
    return detail?.includes("暂无可展示的结构化结果") === true ? "暂无可展示的结构化结果" : null;
  }

  if (event.type === "artifact.created") {
    return artifact === null ? null : `${artifact.title}（${artifact.type}）`;
  }

  if (event.type === "answer.ready") {
    return activity.items.find((item) => item.id === `answer:${activity.runId}`)?.detail ?? null;
  }

  return null;
}

function payloadString(event: RunEvent, key: string): string | null {
  const value = event.payload[key];
  return typeof value === "string" ? value : null;
}

function payloadNumber(event: RunEvent, key: string): number | null {
  const value = event.payload[key];
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function turnLabel(event: RunEvent): string {
  const turnNo = payloadNumber(event, "turn_no");
  return turnNo === null ? "Agent 分析回合" : `第 ${turnNo} 轮 Agent 分析`;
}

function agentTurnAction(event: RunEvent): string {
  const actionKind = payloadString(event, "action_kind");
  const toolNames = (payloadString(event, "tool_names") ?? "")
    .split(",")
    .filter((name) => name.length > 0)
    .map(toolLabel);
  if (actionKind === "model_error") return "模型调用失败";
  if (actionKind === "respond") return "直接生成回复";
  if (toolNames.length > 0) return `调用${toolNames.join("、")}`;
  return "未调用工具";
}

function preparationLabel(event: RunEvent): string {
  switch (payloadString(event, "phase")) {
    case "run_opening":
      return "判断本次处理方式";
    case "semantic_context":
      return "读取数据地图";
    case "analysis_plan":
      return "校验分析计划";
    default:
      return "准备分析";
  }
}

function eventPresentation(event: RunEvent): Pick<RunTraceEntry, "status" | "statusLabel"> {
  switch (event.type) {
    case "run.queued":
      return { status: "queued", statusLabel: "等待中" };
    case "run.started":
      return { status: "running", statusLabel: "已开始" };
    case "run.preparation.started":
      return { status: "running", statusLabel: "进行中" };
    case "run.preparation.completed":
      switch (payloadString(event, "status")) {
        case "timed_out":
          return { status: "failed", statusLabel: "已超时" };
        case "cancelled":
          return { status: "canceled", statusLabel: "已取消" };
        case "failed":
          return { status: "failed", statusLabel: "未完成" };
        default:
          return { status: "succeeded", statusLabel: "已完成" };
      }
    case "run.protocol.selected":
      return { status: "succeeded", statusLabel: "已选择" };
    case "analysis.clarification.requested":
      return { status: "succeeded", statusLabel: "已提出澄清" };
    case "analysis.requirement.blocked":
      return { status: "failed", statusLabel: "受阻" };
    case "analysis.discovery.observed":
      return { status: "succeeded", statusLabel: "已记录" };
    case "agent.turn.started":
      return { status: "running", statusLabel: "已开始" };
    case "agent.turn.completed": {
      const status = payloadString(event, "status");
      if (status === "cancelled") return { status: "canceled", statusLabel: "已取消" };
      if (status === "failed") return { status: "failed", statusLabel: "未完成" };
      return { status: "succeeded", statusLabel: "已完成" };
    }
    case "final_answer.request.started":
      return { status: "running", statusLabel: "进行中" };
    case "final_answer.response.received":
      return { status: "succeeded", statusLabel: "已收到" };
    case "final_answer.validation.failed":
      return { status: "failed", statusLabel: "未通过" };
    case "final_answer.request.timed_out":
      return { status: "failed", statusLabel: "已超时" };
    case "tool.called":
      return { status: "running", statusLabel: "已调用" };
    case "tool.succeeded":
      return { status: "succeeded", statusLabel: "已完成" };
    case "tool.failed":
      return { status: "failed", statusLabel: "未完成" };
    case "artifact.created":
      return { status: "succeeded", statusLabel: "已生成" };
    case "answer.delta":
      return { status: "succeeded", statusLabel: "已记录" };
    case "answer.ready":
      return { status: "succeeded", statusLabel: "已就绪" };
    case "run.cancel_requested":
      return { status: "canceled", statusLabel: "已请求" };
    case "run.succeeded":
      return { status: "succeeded", statusLabel: "已完成" };
    case "run.failed":
      return { status: "failed", statusLabel: "未完成" };
    case "run.canceled":
      return { status: "canceled", statusLabel: "已取消" };
  }
}

function entryTitle(event: RunEvent, context: TraceContext): string {
  const action = context.toolName === null ? "数据工具" : toolLabel(context.toolName);
  switch (event.type) {
    case "run.queued":
      return "运行已进入队列";
    case "run.started":
      return "开始分析";
    case "run.preparation.started":
      return `开始${preparationLabel(event)}`;
    case "run.preparation.completed":
      return payloadString(event, "status") === "timed_out"
        ? `${preparationLabel(event)}超时`
        : payloadString(event, "status") === "cancelled"
          ? `${preparationLabel(event)}已取消`
          : payloadString(event, "status") === "failed"
            ? `${preparationLabel(event)}失败`
            : `${preparationLabel(event)}完成`;
    case "run.protocol.selected":
      return payloadString(event, "protocol_id") === "general-task"
        ? "选择普通问答协议"
        : "选择数据分析协议";
    case "analysis.clarification.requested":
      return "等待用户补充分析范围";
    case "analysis.requirement.blocked":
      return "分析目标受阻";
    case "analysis.discovery.observed":
      return "已记录受限探索观察";
    case "agent.turn.started":
      return `${turnLabel(event)}开始`;
    case "agent.turn.completed":
      return `${turnLabel(event)}${agentTurnAction(event)}`;
    case "final_answer.request.started":
      return "开始生成最终答案";
    case "final_answer.response.received":
      return "收到最终答案模型回复";
    case "final_answer.validation.failed":
      return "最终答案未通过校验";
    case "final_answer.request.timed_out":
      return "最终答案生成超时";
    case "tool.called":
      return `调用${action}`;
    case "tool.succeeded":
      return `${action}执行完成`;
    case "tool.failed":
      return `${action}执行失败`;
    case "artifact.created":
      return `生成产物：${context.artifact?.title ?? payloadString(event, "artifact_type") ?? "分析产物"}`;
    case "answer.delta":
      return "生成答案内容";
    case "answer.ready":
      return "最终答案已就绪";
    case "run.cancel_requested":
      return "收到取消请求";
    case "run.succeeded":
      return "运行完成";
    case "run.failed":
      return "运行失败";
    case "run.canceled":
      return "运行已取消";
  }
}

function completedToolSummary(event: RunEvent, context: TraceContext): string {
  const parts: string[] = [];
  const elapsedMs = payloadNumber(event, "elapsed_ms");
  const evidenceCount = payloadNumber(event, "evidence_count");
  if (elapsedMs !== null) parts.push(`用时 ${elapsedMs} ms`);
  if (evidenceCount !== null) parts.push(`记录 ${evidenceCount} 条证据`);
  if (context.auditCount > 0) parts.push(`关联 ${context.auditCount} 条 SQL 审计`);
  return parts.length === 0 ? "工具执行结果已持久化。" : `${parts.join("，")}。`;
}

function entrySummary(event: RunEvent, context: TraceContext): string {
  switch (event.type) {
    case "run.queued":
      return "本次 Run 已固定输入快照并等待执行。";
    case "run.started":
      return "运行时开始处理本次分析问题。";
    case "run.preparation.started":
      return `正在${preparationLabel(event)}。`;
    case "run.preparation.completed": {
      const elapsedMs = payloadNumber(event, "elapsed_ms");
      const status = payloadString(event, "status");
      if (status === "timed_out" || status === "failed" || status === "cancelled") {
        const reason = payloadString(event, "failure_code");
        return reason === null
          ? `${preparationLabel(event)}未完成。`
          : `${preparationLabel(event)}未完成（${reason}）。`;
      }
      return elapsedMs === null
        ? `${preparationLabel(event)}已完成。`
        : `${preparationLabel(event)}已完成，用时 ${elapsedMs} ms。`;
    }
    case "run.protocol.selected":
      return payloadString(event, "protocol_id") === "general-task"
        ? "本次 Run 将直接回答，不装配数据分析工具。"
        : "本次 Run 将按已校验的分析计划执行。";
    case "analysis.clarification.requested":
      return "当前问题范围不明确，已向用户提出澄清问题。";
    case "analysis.requirement.blocked":
      return `当前目标无法执行（${payloadString(event, "reason_code") ?? "原因未记录"}）。`;
    case "analysis.discovery.observed": {
      const rowCount = payloadNumber(event, "row_count");
      const columnCount = payloadNumber(event, "column_count");
      const size = [
        ...(rowCount === null ? [] : [`${rowCount} 行`]),
        ...(columnCount === null ? [] : [`${columnCount} 列`]),
      ].join("、");
      return `${size.length === 0 ? "探索规模已记录" : `探索返回 ${size}`}；该观察仅用于规划，不是正式证据。`;
    }
    case "agent.turn.started":
      return "模型开始基于当前 Run 事实决定下一步动作。";
    case "agent.turn.completed": {
      const status = payloadString(event, "status");
      const failureCode = payloadString(event, "failure_code");
      const elapsedMs = payloadNumber(event, "elapsed_ms");
      if (status === "cancelled") return "本轮 Agent 已取消。";
      if (status === "failed") {
        return failureCode === null
          ? "本轮 Agent 未完成。"
          : `本轮 Agent 未完成，错误码 ${failureCode}。`;
      }
      const toolCount = payloadNumber(event, "tool_call_count");
      const parts = [
        agentTurnAction(event),
        ...(toolCount === null ? [] : [`${toolCount} 个工具调用`]),
        ...(elapsedMs === null ? [] : [`用时 ${elapsedMs} ms`]),
      ];
      return `${parts.join("，")}。`;
    }
    case "final_answer.request.started":
      return "模型正在根据当前 Run 的安全快照生成最终答案。";
    case "final_answer.response.received": {
      const elapsedMs = payloadNumber(event, "elapsed_ms");
      return elapsedMs === null
        ? "模型回复已收到，正在进行格式和安全校验。"
        : `模型回复已收到，用时 ${elapsedMs} ms，正在进行格式和安全校验。`;
    }
    case "final_answer.validation.failed":
      return payloadString(event, "validation_stage") === "markdown"
        ? "模型已返回结构化答案，但 Markdown 换行或块结构不合规，原文不会展示。"
        : "模型返回的最终答案结构不合规，原文不会展示。";
    case "final_answer.request.timed_out": {
      const elapsedMs = payloadNumber(event, "elapsed_ms");
      return elapsedMs === null
        ? "本次最终答案请求超过阶段时限，已停止。"
        : `本次最终答案请求在 ${elapsedMs} ms 后超过阶段时限，已停止。`;
    }
    case "tool.called":
      return "工具请求已记录，系统将按顺序执行。";
    case "tool.succeeded":
      return completedToolSummary(event, context);
    case "tool.failed": {
      const errorMessage = payloadString(event, "error_message");
      const hint = payloadString(event, "hint");
      const subject = payloadString(event, "subject");
      const line = payloadNumber(event, "line");
      const column = payloadNumber(event, "column");
      const retryable = event.payload.retryable === true;
      if (errorMessage !== null) {
        const location = subject === null
          ? null
          : `${subject}${line === null || column === null ? "" : `（第 ${line} 行，第 ${column} 列）`}`;
        const parts = [errorMessage, ...(location === null ? [] : [location]), ...(hint === null ? [] : [hint])];
        const retryAdvice = retryable ? "系统允许提交一条不同的 SQL 改写。" : "";
        const sentence = parts.join("：");
        return `${/[。！？!?]$/.test(sentence) ? sentence : `${sentence}。`}${retryAdvice}`;
      }
      const errorCode = payloadString(event, "error_code");
      return errorCode === null ? "工具执行未完成。" : `工具执行未完成，错误码 ${errorCode}。`;
    }
    case "artifact.created": {
      const artifactType = context.artifact?.type ?? payloadString(event, "artifact_type");
      return artifactType === null ? "分析产物已经登记。" : `已登记 ${artifactType} 类型产物。`;
    }
    case "answer.delta": {
      const delta = payloadString(event, "delta") ?? "";
      return `已追加 ${delta.length} 个答案字符。`;
    }
    case "answer.ready": {
      const artifactCount = payloadNumber(event, "artifact_count");
      const parts = [
        ...(artifactCount === null ? [] : [`${artifactCount} 个关联产物`]),
      ];
      return parts.length === 0 ? "正式答案已经生成。" : `已生成${parts.join("，")}。`;
    }
    case "run.cancel_requested": {
      const reason = payloadString(event, "reason_code");
      return reason === null ? "系统已记录取消请求。" : `系统已记录取消请求：${reason}。`;
    }
    case "run.succeeded":
      return "唯一成功终态已经写入。";
    case "run.failed": {
      const errorCode = payloadString(event, "error_code");
      return errorCode === null ? "唯一失败终态已经写入。" : `唯一失败终态已经写入：${errorCode}。`;
    }
    case "run.canceled": {
      const errorCode = payloadString(event, "error_code");
      return errorCode === null ? "唯一取消终态已经写入。" : `唯一取消终态已经写入：${errorCode}。`;
    }
  }
}

/** Trace 只重排当前 Run 的持久化事件，不从活动卡或时间邻近关系反推事实。 */
export function deriveRunTrace({
  run,
  events,
  toolCalls,
  sqlAudits,
  artifacts,
}: RunTraceInput): RunTraceEntry[] {
  const activity = deriveRunActivity({ run, events, toolCalls, sqlAudits, artifacts });
  const toolsById = new Map(
    toolCalls.filter((tool) => tool.run_id === run.id).map((tool) => [tool.id, tool]),
  );
  const artifactsById = new Map(
    artifacts.filter((artifact) => artifact.run_id === run.id).map((artifact) => [artifact.id, artifact]),
  );
  const auditCountByTool = new Map<string, number>();
  const sqlAuditsByToolId = new Map<string, SqlAudit[]>();
  const artifactsByToolId = new Map<string, RunArtifact[]>();
  for (const audit of sqlAudits) {
    if (audit.run_id !== run.id || audit.tool_call_id === null) continue;
    auditCountByTool.set(audit.tool_call_id, (auditCountByTool.get(audit.tool_call_id) ?? 0) + 1);
    const current = sqlAuditsByToolId.get(audit.tool_call_id) ?? [];
    current.push(audit);
    sqlAuditsByToolId.set(audit.tool_call_id, current);
  }
  for (const artifact of artifacts) {
    if (artifact.run_id !== run.id || artifact.tool_call_id === null) continue;
    const current = artifactsByToolId.get(artifact.tool_call_id) ?? [];
    current.push(artifact);
    artifactsByToolId.set(artifact.tool_call_id, current);
  }

  return events
    .filter((event) => event.run_id === run.id)
    .sort((left, right) => left.seq - right.seq)
    .map((event) => {
      const toolCallId = payloadString(event, "tool_call_id");
      const toolCall = toolCallId === null ? null : toolsById.get(toolCallId) ?? null;
      const artifactRef = payloadString(event, "artifact_id");
      const artifact = artifactRef === null ? null : artifactsById.get(artifactRef) ?? null;
      const context: TraceContext = {
        toolName: toolCall?.tool_name ?? payloadString(event, "tool_name"),
        toolCall,
        auditCount: toolCallId === null ? 0 : auditCountByTool.get(toolCallId) ?? 0,
        artifact,
      };
      const resultSummary = resultSummaryForEvent(
        event,
        activity,
        artifact,
        toolsById,
        sqlAuditsByToolId,
        artifactsByToolId,
      );
      return {
        id: `trace:${run.id}:${event.seq}`,
        detailId: `event:${event.seq}`,
        seq: event.seq,
        eventType: event.type,
        timestamp: event.timestamp,
        title: entryTitle(event, context),
        summary: entrySummary(event, context),
        ...eventPresentation(event),
        toolCallId,
        artifactId: artifact?.id ?? null,
        resultSummary,
      };
    });
}
