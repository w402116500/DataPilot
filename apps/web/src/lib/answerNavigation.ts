export interface AnswerHeading {
  id: string;
  level: number;
  title: string;
}

/**
 * 只解析最终答案里可稳定定位的 ATX 标题，和渲染器生成的标题顺序保持一致。
 * 代码围栏中的井号不属于答案导航。
 */
export function extractAnswerHeadings(content: string, anchorPrefix: string): AnswerHeading[] {
  const headings: AnswerHeading[] = [];
  let inFence = false;

  for (const line of content.split(/\r?\n/u)) {
    const trimmed = line.trim();
    if (/^(```|~~~)/u.test(trimmed)) {
      inFence = !inFence;
      continue;
    }
    if (inFence) continue;

    const match = /^(#{1,4})[ \t]+(.+?)[ \t]*#*[ \t]*$/u.exec(trimmed);
    if (!match) continue;

    const title = cleanHeadingText(match[2] ?? "");
    if (!title) continue;
    headings.push({
      id: `${anchorPrefix}-heading-${headings.length + 1}`,
      level: match[1]?.length ?? 1,
      title,
    });
  }

  return headings;
}

function cleanHeadingText(value: string): string {
  return value
    .replace(/!\[([^\]]*)\]\([^)]*\)/gu, "$1")
    .replace(/\[([^\]]+)\]\([^)]*\)/gu, "$1")
    .replace(/[`*_~]/gu, "")
    .trim();
}
