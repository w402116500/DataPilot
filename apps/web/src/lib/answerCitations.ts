/** Only standalone citation tokens in Markdown prose; source offsets preserve escapes. */
export function citationTokens(markdown: string): { start: number; end: number; number: number }[] {
  const excluded: [number, number][] = [];
  const referenceDefinitions = new Set<string>();
  let offset = 0;
  let fence: { marker: string; length: number } | null = null;
  for (const line of markdown.split(/(?<=\n)/u)) {
    const marker = /^ {0,3}(`{3,}|~{3,})/u.exec(line);
    if (fence) {
      excluded.push([offset, offset + line.length]);
      if (marker && marker[1]?.[0] === fence.marker && marker[1].length >= fence.length && line.slice(marker[0].length).trim() === "") fence = null;
    } else if (marker) {
      fence = { marker: marker[1]?.[0] ?? "`", length: marker[1]?.length ?? 3 };
      excluded.push([offset, offset + line.length]);
    } else if (/^(?: {4}|\t)|^ {0,3}\[[^\]]+\]:/u.test(line)) {
      excluded.push([offset, offset + line.length]);
      const definition = /^ {0,3}\[([^\]]+)\]:/u.exec(line);
      if (definition?.[1]) referenceDefinitions.add(definition[1]);
    }
    offset += line.length;
  }
  const isExcluded = (index: number): boolean => excluded.some(([start, end]) => index >= start && index < end);
  const escaped = (index: number): boolean => {
    let count = 0;
    for (let i = index - 1; i >= 0 && markdown[i] === "\\"; i -= 1) count += 1;
    return count % 2 === 1;
  };
  const result: { start: number; end: number; number: number }[] = [];
  for (let i = 0; i < markdown.length; i += 1) {
    if (isExcluded(i) || escaped(i)) continue;
    if (markdown[i] === "`") {
      const run = /^`+/u.exec(markdown.slice(i))?.[0] ?? "`";
      const remainder = markdown.slice(i + run.length);
      const closing = [...remainder.matchAll(/`+/gu)].find(match => match[0].length === run.length);
      if (closing) i += run.length + closing.index + closing[0].length - 1;
      else i += run.length - 1;
      continue;
    }
    if (markdown[i] !== "[") continue;
    let depth = 1;
    let close = i + 1;
    for (; close < markdown.length && depth > 0; close += 1) {
      if (escaped(close)) continue;
      if (markdown[close] === "[") depth += 1;
      if (markdown[close] === "]") depth -= 1;
    }
    if (depth !== 0) continue;
    const label = markdown.slice(i + 1, close - 1);
    if (referenceDefinitions.has(label)) {
      i = close - 1;
      continue;
    }
    if (markdown[close] === "(") {
      let nesting = 1;
      let end = close + 1;
      for (; end < markdown.length && nesting > 0; end += 1) {
        if (escaped(end)) continue;
        if (markdown[end] === "(") nesting += 1;
        if (markdown[end] === ")") nesting -= 1;
      }
      i = end - 1;
      continue;
    }
    const nextLabel = /^\[([^\]]*)\]/u.exec(markdown.slice(close));
    if (nextLabel && !(/^[1-9]\d*$/u.test(label) && /^[1-9]\d*$/u.test(nextLabel[1] ?? ""))) {
      i = close + nextLabel[0].length - 1;
      continue;
    }
    if (markdown[i - 1] === "!" || !/^[1-9]\d*$/u.test(label)) { i = close - 1; continue; }
    // Match the server's capped value for oversized labels without losing precision in JS.
    result.push({ start: i, end: close, number: label.length > 15 ? 10 ** 15 : Number(label) });
    i = close - 1;
  }
  return result;
}

export function citationLinkPrefix(markdown: string): string {
  let prefix = "#answer-citation-";
  while (markdown.includes(prefix)) prefix += "ref-";
  return prefix;
}

export function renderCitationLinks(markdown: string, allowed: readonly number[], prefix = citationLinkPrefix(markdown)): string {
  const numbers = new Set(allowed);
  let result = markdown;
  for (const token of citationTokens(markdown).reverse()) {
    if (numbers.has(token.number)) {
      result = result.slice(0, token.start) + `[\\[${token.number}\\]](${prefix}${token.number})` + result.slice(token.end);
    }
  }
  return result;
}
