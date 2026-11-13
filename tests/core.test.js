// Unit tests for runtime/core.js — run with `node --test`.
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  COMMAND_RAN,
  DEFAULT_REFRESH_MS,
  WidgetController,
  defaultUpdate,
  evaluateWidget,
  refreshDelay,
} from "../runtime/core.js";

/** Manual timers: nothing fires until fire() is called. */
function fakeTimers() {
  let next = 1;
  const pending = new Map();
  return {
    setTimeout(fn, ms) {
      const id = next++;
      pending.set(id, { fn, ms });
      return id;
    },
    clearTimeout(id) {
      pending.delete(id);
    },
    get pending() {
      return [...pending.values()];
    },
    fire() {
      const all = [...pending.values()];
      pending.clear();
      all.forEach((t) => t.fn());
    },
  };
}

/** run() whose results are resolved by hand, to control ordering. */
function manualRun() {
  const calls = [];
  const run = (cmd) =>
    new Promise((resolve, reject) => {
      calls.push({ cmd, resolve, reject });
    });
  return { run, calls };
}

const flush = () => new Promise((r) => setImmediate(r));

function make(mod, extra = {}) {
  const renders = [];
  const errors = [];
  const timers = fakeTimers();
  const { run, calls } = manualRun();
  const ctl = new WidgetController({
    id: "w",
    mod,
    run,
    timers,
    render: (state) => renders.push(state),
    onError: (where, e) => errors.push([where, String(e)]),
    ...extra,
  });
  return { ctl, renders, errors, timers, calls };
}

test("refreshDelay follows Übersicht defaults", () => {
  assert.equal(refreshDelay({}), DEFAULT_REFRESH_MS);
  assert.equal(refreshDelay({ refreshFrequency: 5000 }), 5000);
  assert.equal(refreshDelay({ refreshFrequency: false }), null);
  assert.equal(refreshDelay({ refreshFrequency: 0 }), null);
  assert.equal(refreshDelay({ refreshFrequency: "junk" }), null);
});

test("defaultUpdate merges output and error only for command events", () => {
  const prev = { output: "a", error: null, keep: 1 };
  assert.deepEqual(defaultUpdate({ type: COMMAND_RAN, output: "b", error: null }, prev), {
    output: "b",
    error: null,
    keep: 1,
  });
  assert.equal(defaultUpdate({ type: "OTHER" }, prev), prev);
});

test("evaluateWidget returns CommonJS exports and passes require", () => {
  const mod = evaluateWidget('exports.x = require("uebersicht").v + 1;', "id", (n) => ({ v: n.length }));
  assert.equal(mod.x, "uebersicht".length + 1);
});

test("string command: run → UB/COMMAND_RAN → render, then reschedule", async () => {
  const { ctl, renders, timers, calls } = make({ command: "date", refreshFrequency: 3000 });
  ctl.start();
  assert.deepEqual(renders, [{ output: "", error: null }]);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].cmd, "date");

  calls[0].resolve("12:00\n");
  await flush();
  assert.deepEqual(renders.at(-1), { output: "12:00\n", error: null });
  assert.deepEqual(timers.pending.map((t) => t.ms), [3000]);

  timers.fire();
  assert.equal(calls.length, 2);
});

test("failed command reports error and stdout", async () => {
  const { ctl, renders, calls } = make({ command: "false" });
  ctl.start();
  const err = new Error("boom");
  err.stdout = "partial";
  calls[0].reject(err);
  await flush();
  assert.deepEqual(renders.at(-1), { output: "partial", error: "boom" });
});

test("refreshFrequency false runs the command once", async () => {
  const { ctl, timers, calls } = make({ command: "x", refreshFrequency: false });
  ctl.start();
  calls[0].resolve("");
  await flush();
  assert.equal(timers.pending.length, 0);
});

test("manual refresh while a command runs does not start a second loop", async () => {
  const { ctl, timers, calls, renders } = make({ command: "slow", refreshFrequency: 1000 });
  ctl.start();
  ctl.refresh(); // e.g. the ⟳ button, while the first run is in flight
  assert.equal(calls.length, 2);

  calls[0].resolve("stale");
  calls[1].resolve("fresh");
  await flush();
  assert.equal(timers.pending.length, 1, "exactly one refresh timer");
  assert.ok(!renders.some((s) => s.output === "stale"), "stale result is dropped");
  assert.equal(renders.at(-1).output, "fresh");
});

test("function command receives dispatch and is rescheduled", () => {
  let got = null;
  const { ctl, timers } = make({
    command: (dispatch) => {
      got = dispatch;
    },
    refreshFrequency: 500,
  });
  ctl.start();
  assert.equal(typeof got, "function");
  assert.deepEqual(timers.pending.map((t) => t.ms), [500]);
});

test("initialState, updateState and init(dispatch)", () => {
  const { ctl, renders } = make({
    initialState: { n: 0 },
    updateState: (e, prev) => (e.type === "INC" ? { n: prev.n + 1 } : prev),
    init: (dispatch) => dispatch({ type: "INC" }),
  });
  ctl.start();
  ctl.dispatch({ type: "INC" });
  assert.deepEqual(renders, [{ n: 0 }, { n: 1 }, { n: 2 }]);
});

test("errors in init/updateState/command are reported, not thrown", () => {
  const { ctl, errors } = make({
    init: () => {
      throw new Error("init!");
    },
    updateState: () => {
      throw new Error("update!");
    },
    command: () => {
      throw new Error("cmd!");
    },
  });
  ctl.start();
  ctl.dispatch({ type: "X" });
  assert.deepEqual(
    errors.map(([w]) => w),
    ["init", "command", "updateState"]
  );
});

test("destroy stops timers and ignores late results", async () => {
  const { ctl, timers, calls, renders } = make({ command: "x" });
  ctl.start();
  ctl.destroy();
  calls[0].resolve("late");
  await flush();
  assert.equal(timers.pending.length, 0);
  assert.equal(renders.length, 1);
});
