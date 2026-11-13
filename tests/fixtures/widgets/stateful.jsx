// Mirrors the shape of real Übersicht widgets: imports from "uebersicht",
// a relative helper, initialState/updateState, render(state, dispatch).
import { run, css } from "uebersicht";
import { label } from "./_helper.js";

export const command = "echo hi";
export const refreshFrequency = false;
export const className = `top: 10px; left: 10px;`;
export const initialState = { output: "", n: 0 };
export const updateState = (event, prev) =>
  event.type === "INC" ? { ...prev, n: prev.n + 1 } : { ...prev, output: event.output };
export const box = css`color: red;`;
export const render = ({ output, n }, dispatch) => (
  <>
    <b onClick={() => dispatch({ type: "INC" })}>{label(n)}</b>
    <span>{output}</span>
  </>
);
export const ran = run;
