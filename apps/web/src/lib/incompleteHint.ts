const INCOMPLETE_HINTS: Record<string, string> = {
  FINAL_ANSWER_INVALID: "最终答案的证据不完整。",
  OUTCOME_INCOMPLETE: "没有形成完整的可验证结论。",
  RUN_TIMEOUT: "分析达到总时限。",
  ANALYSIS_CLAIM_COMMIT_TIMEOUT: "结论提交阶段达到本次时限。",
  ANALYSIS_AGENT_TURN_TIMEOUT: "分析回合达到单轮时限，已保存当前已完成结果。",
  FINAL_ANSWER_TIMEOUT: "最终回答生成达到本次时限。",
  FINAL_ANSWER_FACT_MISMATCH: "最终答案中的数字与已验证事实不一致。",
  TOOL_CALL_LIMIT_REACHED: "工具调用达到本次上限。",
  TURN_LIMIT_REACHED: "分析回合达到本次上限。",
  ANALYSIS_REQUIREMENTS_QUERY_REQUIRED: "仍有用户问题未查询，尚不能形成完整结论。",
  ANALYSIS_REQUIREMENTS_COMMIT_REQUIRED: "已取得可验证结果，但结论尚未按当前证据提交。",
  ARTIFACT_DELIVERABLE_REQUIRED: "已完成数据结论，但尚未生成用户要求的正式产物。",
  ANALYSIS_CLAIM_COMMIT_RETRY_EXHAUSTED:
    "已取得查询结果，但模型两次未能按当前证据提交结论。",
  CONTEXT_BUDGET_EXHAUSTED: "上下文预算不足，最终回答未生成。",
};

export function incompleteHint(reason: string | null | undefined): string {
  return INCOMPLETE_HINTS[reason ?? ""] ?? "本次回答不完整，请结合过程和产物复核。";
}
