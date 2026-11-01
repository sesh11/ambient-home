import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { runInNewContext } from "node:vm";

const html = readFileSync(new URL("../src/ambient_home/ui/index.html", import.meta.url), "utf8");
const renderSource = html.slice(html.indexOf("    function renderJobs("), html.indexOf("    async function refresh("));

function fixture() {
  const document = { activeElement: null };
  const body = {
    rows: [],
    querySelectorAll: () => [...body.rows],
    insertBefore(row, next) {
      if (row.parentNode) row.remove();
      const index = next === null ? body.rows.length : body.rows.indexOf(next);
      assert.ok(index >= 0);
      body.rows.splice(index, 0, row);
      row.parentNode = body;
    },
  };
  const createJobRow = job => {
    const input = { value: "", selectionStart: 0, selectionEnd: 0 };
    return {
      dataset: { jobId: job.id }, input, parentNode: null,
      contains: node => node === input,
      get nextElementSibling() {
        return body.rows[body.rows.indexOf(this) + 1] || null;
      },
      remove() {
        if (this.contains(document.activeElement)) document.activeElement = null;
        this.parentNode.rows.splice(this.parentNode.rows.indexOf(this), 1);
        this.parentNode = null;
      },
    };
  };
  document.getElementById = id => {
    assert.equal(id, "jobs");
    return body;
  };
  const render = runInNewContext(renderSource + "\nrenderJobs", {
    document, createJobRow, updateJobRow: (row, job) => { row.job = job; },
  });
  return { document, body, render };
}

test("polling preserves the answer editor, selection and focus", () => {
  const { document, body, render } = fixture();
  const jobs = [{ id: "blocked", status: "blocked" }, { id: "done", status: "finished" }];
  render(jobs);
  const row = body.rows[0];
  row.input.value = "Yes, use temporary storage only.";
  row.input.selectionStart = 5;
  row.input.selectionEnd = 12;
  document.activeElement = row.input;
  for (let i = 0; i < 5; i++) render(jobs);
  assert.equal(document.activeElement, row.input);
  assert.equal(body.rows[0], row);
  assert.equal(row.input.value, "Yes, use temporary storage only.");
  assert.equal(row.input.selectionStart, 5);
  assert.equal(row.input.selectionEnd, 12);
});

test("new, reordered, updated and removed jobs preserve a surviving editor", () => {
  const { document, body, render } = fixture();
  render([{ id: "a" }, { id: "blocked" }, { id: "stale" }]);
  const row = body.rows[1];
  row.input.value = "draft";
  document.activeElement = row.input;
  for (const ids of [
    ["blocked", "new", "a"], ["new", "a", "blocked"], ["a", "blocked", "new"],
  ]) {
    render(ids.map(id => ({ id, status: "working" })));
    assert.deepEqual(body.rows.map(item => item.dataset.jobId), ids);
    assert.equal(document.activeElement, row.input);
    assert.equal(row.input.value, "draft");
    assert.equal(row.job.status, "working");
  }
  render([]);
  assert.equal(body.rows.length, 0);
  assert.equal(document.activeElement, null);
});
