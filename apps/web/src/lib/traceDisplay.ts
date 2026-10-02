import type { TraceDagActionRecord, TraceDagNode, TraceDagSection } from "@/api/types";

import { toolLabel } from "./runActivity";

const preparationLabels: Record<string, string> = {
  run_opening: "判断处理方式",
  semantic_context: "读取数据地图",
  analysis_plan: "校验分析计划",
};

export function traceNodeLabel(node: TraceDagNode): string {
  if (node.kind === "run-start" && node.label === "Run start") return "运行开始";
  if (node.kind === "agent-turn" && node.turn_no !== null && node.label === `Agent turn ${node.turn_no}`) {
    return `第 ${node.turn_no} 轮分析`;
  }
  if (node.kind === "preparation") {
    const phase = node.detail.phase;
    if (typeof phase === "string" && node.label === `Preparation · ${phase.replace(/_/g, " ")}`) {
      return preparationLabels[phase] ?? node.label;
    }
    if (node.label === "Preparation") return "分析准备";
  }
  if (node.kind === "tool") {
    const name = node.detail.tool_name;
    if (typeof name === "string" && node.label === name.replace(/_/g, " ")
      && ["run_sql_readonly", "run_python", "explore_datalink", "commit_analysis_claims"].includes(name)) {
      return toolLabel(name);
    }
  }
  if (node.kind === "final-answer" && node.label === "Final answer") return "生成回答";
  if (node.kind === "run-terminal" && node.label === `Run ${node.status}`) return "运行结束";
  return node.label;
}

export function traceSectionLabel(section: TraceDagSection, nodes: readonly TraceDagNode[]): string {
  const members = nodes.filter((node) => section.node_ids.includes(node.id));
  const turn = members.find((node) => node.turn_no !== null && section.title === `Agent turn ${node.turn_no}`);
  if (turn) return `第 ${turn.turn_no} 轮分析`;
  const source = members.find((node) => node.label === section.title);
  if (!source) return section.title;
  if (source.kind === "preparation" && section.id === `run:${source.run_id}:section:preparation`
    && traceNodeLabel(source) !== source.label) return "分析准备";
  return traceNodeLabel(source);
}

export function traceActionLabel(action: TraceDagActionRecord): string {
  const labels: Record<TraceDagActionRecord["kind"], string> = {
    run_started: "运行开始",
    protocol_selected: "确认处理方式",
    preparation_started: "开始准备",
    preparation_completed: "准备结束",
    turn_started: "开始分析",
    turn_completed: "分析结束",
    tool_requested: "请求工具",
    tool_completed: "工具返回",
    discovery_observed: "记录数据发现",
    artifact_registered: "产物已登记",
    claim_committed: "结论已提交",
    answer_requested: "开始生成回答",
    answer_validated: "回答已校验",
    terminal_recorded: "记录运行结果",
  };
  return labels[action.kind];
}
