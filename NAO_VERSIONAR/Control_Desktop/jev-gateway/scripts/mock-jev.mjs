#!/usr/bin/env node
// mock-jev: a local stand-in for TypeSafe's `POST /v1/systemone`, for driving the router
// end to end (real client, real upstream) without a TypeSafe key. The SDK honours
// TYPESAFE_BASE_URL, so:
//
//   MOCK_JEV_SCRIPT=exec_command,no_tool_needed node scripts/mock-jev.mjs &
//   TYPESAFE_BASE_URL=http://127.0.0.1:8799 TYPESAFE_API_KEY=mock jev-codex exec "…"
//
// It answers whatever question keys it is sent (see src/questions.ts): scripted for `tool`,
// consistent for `needs_tool`, deliberately unsure for arguments.
import { createServer } from "node:http";
import { mkdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";

const PORT = Number(process.env.MOCK_JEV_PORT ?? 8799);
const NO_TOOL = "no_tool_needed";
// One `tool` answer per request, in order; the last one repeats. An agent loop needs this:
// "always pick the shell" never lets the turn end.
const SCRIPT = (process.env.MOCK_JEV_SCRIPT ?? NO_TOOL).split(",").map((name) => name.trim()).filter(Boolean);
const CONFIDENCE = Number(process.env.MOCK_JEV_CONFIDENCE ?? 0.95);
// Argument answers below the router's JEV_ARG_MIN_CERTAINTY keep it out of `direct` mode;
// raise this to exercise direct calls.
const ARG_CERTAINTY = Number(process.env.MOCK_JEV_ARG_CERTAINTY ?? 0.5);
const DUMP_DIR = process.env.MOCK_JEV_DUMP_DIR;

let served = 0;

function answer(key, question, wanted) {
  if (key === "tool") {
    const options = Object.keys(question.criteria ?? {});
    // A scripted name the client doesn't offer would be a mock bug, not a routing result.
    const choice = options.includes(wanted) ? wanted : options.includes(NO_TOOL) ? NO_TOOL : options[0];
    const rest = (1 - CONFIDENCE) / Math.max(1, options.length - 1);
    const probabilities = Object.fromEntries(options.map((name) => [name, name === choice ? CONFIDENCE : rest]));
    return { type: "choice", choice, confidence: CONFIDENCE, probabilities };
  }
  if (key.startsWith("shard:")) {
    // First pass over a big roster: the wanted tool lives in exactly one shard.
    const options = Object.keys(question.criteria ?? {});
    const choice = options.includes(wanted) ? wanted : "none_of_these";
    const rest = (1 - CONFIDENCE) / Math.max(1, options.length - 1);
    return { type: "choice", choice, confidence: CONFIDENCE, probabilities: Object.fromEntries(options.map((name) => [name, name === choice ? CONFIDENCE : rest])) };
  }
  if (key === "needs_tool") return { type: "noul", noul: wanted === NO_TOOL ? 0.1 : 0.9 };
  if (question.type === "noul") return { type: "noul", noul: ARG_CERTAINTY };
  if (question.type === "score") return { type: "score", score: 0.5 };
  const options = Object.keys(question.criteria ?? {});
  return {
    type: "choice",
    choice: options[0],
    confidence: ARG_CERTAINTY,
    probabilities: Object.fromEntries(options.map((name, i) => [name, i === 0 ? ARG_CERTAINTY : 0])),
  };
}

createServer(async (req, res) => {
  const chunks = [];
  for await (const chunk of req) chunks.push(chunk);
  const send = (status, body) => {
    res.writeHead(status, { "content-type": "application/json" });
    res.end(JSON.stringify(body));
  };
  if (req.method !== "POST" || !req.url?.startsWith("/v1/systemone")) return send(404, { error: "mock-jev only serves POST /v1/systemone" });

  let request;
  try {
    request = JSON.parse(Buffer.concat(chunks).toString("utf8"));
  } catch {
    return send(400, { error: "invalid JSON" });
  }
  const questions = request.questions ?? {};
  const wanted = SCRIPT[Math.min(served, SCRIPT.length - 1)];
  // A shortlist pass and the decision that follows it belong to the same turn of the script.
  if ("tool" in questions) served++;
  const answers = Object.fromEntries(Object.entries(questions).map(([key, q]) => [key, answer(key, q, wanted)]));
  const state = JSON.stringify(request.state ?? null);

  if (DUMP_DIR) {
    mkdirSync(DUMP_DIR, { recursive: true });
    writeFileSync(join(DUMP_DIR, `jev-${String(served).padStart(4, "0")}.json`), JSON.stringify({ request, answers }, null, 2));
  }
  console.log(
    JSON.stringify({
      n: served,
      scripted: wanted,
      answered: answers.tool?.choice,
      options: Object.keys(questions.tool?.criteria ?? {}),
      questions: Object.keys(questions).length,
      stateChars: state.length,
    }),
  );
  send(200, { model: request.model ?? "mock-jev", answers, usage: { input_tokens: Math.ceil(state.length / 4), output_tokens: 0 } });
}).listen(PORT, "127.0.0.1", () => console.log(`mock-jev on http://127.0.0.1:${PORT} — script: ${SCRIPT.join(" → ")}`));
