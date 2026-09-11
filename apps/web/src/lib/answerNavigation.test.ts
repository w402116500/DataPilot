import { describe, expect, it } from "vitest";

import { extractAnswerHeadings } from "./answerNavigation";

describe("extractAnswerHeadings", () => {
  it("提取标题层级并生成稳定锚点", () => {
    const headings = extractAnswerHeadings(
      [
        "# 总结",
        "",
        "正文里的 # 不是标题",
        "## `关键` [发现](https://example.com)",
        "",
        "```markdown",
        "# 代码示例",
        "```",
      ].join("\n"),
      "answer-message-1",
    );

    expect(headings).toEqual([
      { id: "answer-message-1-heading-1", level: 1, title: "总结" },
      { id: "answer-message-1-heading-2", level: 2, title: "关键 发现" },
    ]);
  });

  it("忽略空标题和不支持的五级标题", () => {
    expect(extractAnswerHeadings("##### 过深\n##   \n", "answer")).toEqual([]);
  });
});
