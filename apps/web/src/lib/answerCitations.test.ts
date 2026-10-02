import { describe, expect, it } from "vitest";
import fixtures from "../../../../tests/fixtures/answer_citations.json";
import { citationTokens, renderCitationLinks } from "./answerCitations";

describe("answer citations", () => {
  for (const fixture of fixtures) {
    it(fixture.name, () => {
      expect([...new Set(citationTokens(fixture.markdown).map(item => item.number))]).toEqual(fixture.numbers);
    });
  }
  it("keeps unknown citations plain and escaped examples untouched", () => {
    expect(renderCitationLinks("引用[1]、[9]和\\[1]", [1])).toBe("引用[\\[1\\]](#answer-citation-1)、[9]和\\[1]");
  });
  it("matches the server for oversized citation labels", () => {
    expect(citationTokens("[1000000000000001]").map(token => token.number)).toEqual([10 ** 15]);
  });
});
