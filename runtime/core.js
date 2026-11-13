// DOM-free core of the widget runtime: module evaluation, state machine and
// command scheduling. Kept separate from runtime.jsx so it can be unit-tested
// with plain `node --test`.

export const DEFAULT_REFRESH_MS = 1000; // Übersicht's default
export const COMMAND_RAN = "UB/COMMAND_RAN";

/** Default updateState: merge command output/error into the state. */
export const defaultUpdate = (event, prev) =>
  event && ("output" in event || "error" in event)
    ? { ...prev, output: event.output, error: event.error }
    : prev;

export const defaultState = () => ({ output: "", error: null });

/** Evaluate a CommonJS bundle produced by esbuild and return its exports. */
export function evaluateWidget(code, id, require) {
  const module = { exports: {} };
  const fn = new Function("module", "exports", "require", `${code}\n//# sourceURL=widget://${id}`);
  fn(module, module.exports, require);
  return module.exports;
}

/** Refresh interval in ms, or null when the command should run only once. */
export function refreshDelay(mod) {
  const f = mod.refreshFrequency === undefined ? DEFAULT_REFRESH_MS : mod.refreshFrequency;
  if (f === false || f === null) return null;
  const n = Number(f);
  return Number.isFinite(n) && n > 0 ? n : null;
}

/**
 * Drives one widget: holds its state, dispatches events through updateState,
 * runs its command on schedule and asks the host to re-render.
 *
 *   run(cmd)              → Promise<stdout>, rejects with Error (optionally .stdout)
 *   render(state, dispatch) host callback, called after every state change
 *   onError(where, error)   host callback for errors in widget code
 */
export class WidgetController {
  constructor({ id, mod, run, render, onError, timers }) {
    this.id = id;
    this.mod = mod;
    this.run = run;
    this.renderHost = render;
    this.onError = onError || ((where, e) => console.error(id, where, e));
    // Wrap the globals: browsers throw "Illegal invocation" when setTimeout/clearTimeout
    // are called as methods of another object.
    this.timers = timers || { setTimeout: (f, ms) => setTimeout(f, ms), clearTimeout: (id) => clearTimeout(id) };
    this.alive = true;
    this.timer = null;
    this.generation = 0; // bumps on every refresh(); stale command results won't reschedule
    this.state = mod.initialState !== undefined ? mod.initialState : defaultState();
    this.dispatch = (event) => this.handle(event);
  }

  start() {
    this.renderHost(this.state, this.dispatch);
    if (typeof this.mod.init === "function") {
      try {
        this.mod.init(this.dispatch);
      } catch (e) {
        this.onError("init", e);
      }
    }
    this.refresh();
  }

  handle(event) {
    if (!this.alive) return;
    try {
      this.state = (this.mod.updateState || defaultUpdate)(event, this.state);
    } catch (e) {
      this.onError("updateState", e);
    }
    this.renderHost(this.state, this.dispatch);
  }

  refresh() {
    if (!this.alive) return;
    this.timers.clearTimeout(this.timer);
    this.timer = null;
    const gen = ++this.generation;
    const delay = refreshDelay(this.mod);
    const scheduleNext = () => {
      if (this.alive && gen === this.generation && delay !== null) {
        this.timer = this.timers.setTimeout(() => this.refresh(), delay);
      }
    };
    const { command } = this.mod;
    if (typeof command === "function") {
      try {
        command(this.dispatch);
      } catch (e) {
        this.onError("command", e);
      }
      scheduleNext();
      return Promise.resolve();
    }
    if (typeof command === "string" && command.trim()) {
      return this.run(command)
        .then(
          (output) => {
            if (gen === this.generation) this.handle({ type: COMMAND_RAN, output, error: null });
          },
          (e) => {
            if (gen === this.generation) {
              this.handle({ type: COMMAND_RAN, output: (e && e.stdout) || "", error: String((e && e.message) || e) });
            }
          }
        )
        .finally(scheduleNext);
    }
    return Promise.resolve();
  }

  destroy() {
    this.alive = false;
    this.timers.clearTimeout(this.timer);
    this.timer = null;
  }
}
