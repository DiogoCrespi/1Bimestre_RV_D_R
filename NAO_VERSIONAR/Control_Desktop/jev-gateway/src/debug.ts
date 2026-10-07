import { mkdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";

/**
 * Opt-in wire dumps (`JEV_DEBUG_DUMP_DIR`): what a client *really* sends is the only reliable
 * spec for backends like ChatGPT's, which are undocumented. Dumps hold the whole conversation,
 * so they are never on by default and credentials never reach the disk.
 */

// Anything that authenticates or identifies the account. Matched loosely on purpose: a header
// redacted by mistake costs nothing, one leaked by mistake is a credential on disk.
const SECRET_HEADER = /auth|cookie|token|secret|key|account|session|signature/i;

export function redactHeaders(headers: Headers): Record<string, string> {
  const out: Record<string, string> = {};
  headers.forEach((value, name) => {
    out[name] = SECRET_HEADER.test(name) ? `[redacted, ${value.length} chars]` : value;
  });
  return out;
}

export type Dump = (kind: string, data: Record<string, unknown>) => void;

// A stream can end without completing; clients (Codex: "Reconnecting…") treat that as a failure.
const TERMINAL = new Set(["response.completed", "response.incomplete", "response.failed"]);

type OutputItem = { type?: string; name?: string; namespace?: string };

/** What a Responses reply (SSE or JSON) says about itself: enough to see cache hits and tool calls. */
export function summarizeResponse(text: string): Record<string, unknown> {
  let done: Record<string, unknown> | undefined;
  let ending: string | undefined;
  // "responses-lite" streams leave `response.output` empty; the items only appear as events.
  const streamed: OutputItem[] = [];
  const parse = (json: string) => {
    try {
      const event = JSON.parse(json) as { type?: string; response?: Record<string, unknown>; item?: OutputItem };
      if (event.type === "response.output_item.done" && event.item) streamed.push(event.item);
      else if (event.type && TERMINAL.has(event.type)) [ending, done] = [event.type, event.response];
      else if (!event.type) done = event as Record<string, unknown>;
    } catch {
      // A chunk cut short by the client hanging up.
    }
  };
  if (text.trimStart().startsWith("{")) parse(text);
  else for (const line of text.split("\n")) if (line.startsWith("data:")) parse(line.slice(5));

  if (!done) return { unparsed: text.slice(-2_000) };
  const { attribution: _perItem, ...usage } = (done.usage ?? {}) as Record<string, unknown>;
  const output = streamed.length ? streamed : Array.isArray(done.output) ? (done.output as OutputItem[]) : [];
  return {
    ...(ending && ending !== "response.completed"
      ? { ending, incomplete_details: done.incomplete_details, error: done.error }
      : {}),
    model: done.model,
    tool_choice: done.tool_choice,
    usage,
    // Runs of one item type collapse to a count: a derailed reply can hold a hundred of them.
    output: output.reduce<(OutputItem & { count?: number })[]>((runs, { type, name, namespace }) => {
      const last = runs.at(-1);
      if (last && last.type === type && last.name === name) last.count = (last.count ?? 1) + 1;
      else runs.push({ type, name, namespace });
      return runs;
    }, []),
  };
}

/** Writes `<dir>/<start>-<seq>-<kind>.json`, never throwing; undefined (off) without a directory. */
export function createDump(dir: string | undefined): Dump | undefined {
  if (!dir) return undefined;
  const startedAt = Date.now();
  let sequence = 0;
  return (kind, data) => {
    try {
      mkdirSync(dir, { recursive: true });
      const name = `${startedAt}-${String(++sequence).padStart(4, "0")}-${kind}.json`;
      writeFileSync(join(dir, name), JSON.stringify(data, null, 2), { mode: 0o600 });
    } catch {
      // Debugging aid only: a full disk must not break routing.
    }
  };
}
