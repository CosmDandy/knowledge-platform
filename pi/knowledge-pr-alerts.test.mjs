import assert from "node:assert/strict";
import test from "node:test";
import alerts from "./knowledge-pr-alerts.ts";

function setup() {
  const notifications = [];
  let handler;
  alerts({ on(name, fn) {
    assert.equal(name, "tool_result");
    handler = fn;
  } });
  return {
    notifications,
    emit(proposal, event = {}, hasUI = true) {
      handler({
        toolName: "mcp__knowledge__propose_note",
        structuredContent: { structuredContent: { result: JSON.stringify(proposal) } },
        ...event,
      }, { hasUI, ui: { notify: (...args) => notifications.push(args) } });
    },
  };
}

const proposal = (open, overrides) => ({ new_pr: true, open_prs: open, max_open_prs: 30, ...overrides });

test("notifies at 10, 20, and capacity, including nested MCP calls", () => {
  const { emit, notifications } = setup();
  for (const open of [9, 10, 11, 19, 20, 21, 29, 30]) {
    emit(proposal(open), {
      toolName: open === 20 ? "mcp__knowledge__propose_edit" : "mcp__knowledge__propose_note",
      parentToolCallId: "codemode",
    });
  }
  assert.deepEqual(notifications.map(([message, level]) => [message.match(/\d+\/30/)[0], level]), [
    ["10/30", "warning"], ["20/30", "warning"], ["30/30", "warning"],
  ]);
  assert.match(notifications[2][0], /before opening a new topic/);
});

test("ignores existing PRs, failed calls, missing counts and non-interactive sessions", () => {
  const { emit, notifications } = setup();
  emit(proposal(10, { new_pr: false }));
  emit(proposal(20), { isError: true });
  emit({ error: "30 proposals are already open" });
  emit({ new_pr: true });
  emit(null);
  emit(proposal(10), { structuredContent: { structuredContent: { result: "not JSON" } } });
  emit(proposal(10), { toolName: "mcp__knowledge__list_proposals" });
  emit(proposal(30), {}, false);
  assert.deepEqual(notifications, []);
});

test("warns when a custom limit fills", () => {
  const { emit, notifications } = setup();
  emit(proposal(5, { max_open_prs: 5 }));
  assert.match(notifications[0][0], /5\/5.*before opening a new topic/);
});
