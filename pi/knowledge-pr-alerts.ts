import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

export default function (pi: ExtensionAPI) {
  pi.on("tool_result", (event, ctx) => {
    if (!ctx.hasUI || event.isError || (
      event.toolName !== "mcp__knowledge__propose_note" &&
      event.toolName !== "mcp__knowledge__propose_edit"
    )) return;

    const result = event.structuredContent?.structuredContent;
    if (typeof result?.result !== "string") return;

    let proposal;
    try {
      proposal = JSON.parse(result.result);
    } catch {
      return;
    }
    const { new_pr, open_prs: open, max_open_prs: limit } = proposal ?? {};
    if (new_pr !== true || !Number.isSafeInteger(open) || !Number.isSafeInteger(limit)) return;

    if (open === limit) {
      ctx.ui.notify(`Knowledge: ${open}/${limit} PRs open. Review or close a PR before opening a new topic.`, "warning");
    } else if (open === 10 || open === 20 || open === 30) {
      ctx.ui.notify(`Knowledge: ${open}/${limit} PRs open and waiting for review.`, "warning");
    }
  });
}
