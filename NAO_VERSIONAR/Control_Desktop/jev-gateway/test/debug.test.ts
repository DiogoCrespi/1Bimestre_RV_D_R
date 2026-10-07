import { mkdtempSync, readdirSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, describe, expect, it } from "vitest";
import { createApp } from "../src/app.js";
import { createDump, redactHeaders, summarizeResponse } from "../src/debug.js";
import { forward } from "../src/upstream.js";
import { fakeJev, testConfig } from "./helpers.js";

const sseOf = (events: Record<string, unknown>[]) =>
  events.map((event) => `event: ${event.type}\ndata: ${JSON.stringify(event)}\n\n`).join("");

describe("debug dumps (JEV_DEBUG_DUMP_DIR)", () => {
  let dir: string | undefined;
  afterEach(() => dir && rmSync(dir, { recursive: true, force: true }));

  it("keeps credentials and account identifiers off the disk", () => {
    const headers = new Headers({
      authorization: "Bearer eyJsecret",
      "chatgpt-account-id": "acct-1234",
      cookie: "cf=1",
      "session-id": "s-1",
      originator: "codex_exec",
    });
    expect(redactHeaders(headers)).toEqual({
      authorization: "[redacted, 16 chars]",
      "chatgpt-account-id": "[redacted, 9 chars]",
      cookie: "[redacted, 4 chars]",
      "session-id": "[redacted, 3 chars]",
      originator: "codex_exec",
    });
  });

  it("writes the decoded request, the rewrite and the reply's usage; nothing when off", async () => {
    expect(createDump(undefined)).toBeUndefined();
    dir = mkdtempSync(join(tmpdir(), "jev-dump-"));
    const app = createApp({
      config: testConfig(),
      askJev: fakeJev({ tool: { choice: "shell" }, needs_tool: { noul: 0.9 } }).askJev,
      dump: createDump(dir),
      fetch: (async () =>
        new Response(
          sseOf([
            { type: "response.output_item.done", item: { type: "function_call", name: "shell", arguments: "{}" } },
            // Lite streams finish with an empty `output`; per-item usage attribution is noise.
            { type: "response.completed", response: { model: "m", tool_choice: "auto", output: [], usage: { input_tokens: 9, attribution: {} } } },
          ]),
        )) as unknown as typeof fetch,
    });
    await app.request("/v1/responses", {
      method: "POST",
      headers: { authorization: "Bearer eyJsecret" },
      body: JSON.stringify({
        model: "m",
        input: "list files",
        tools: [{ type: "function", name: "shell", parameters: { type: "object", properties: { command: { type: "string" } } } }],
        stream: true,
      }),
    });
    await expect.poll(() => readdirSync(dir!).length).toBe(2);

    const [request, response] = readdirSync(dir)
      .sort()
      .map((name) => JSON.parse(readFileSync(join(dir!, name), "utf8")));
    expect(request).toMatchObject({ path: "/v1/responses", body: { input: "list files" } });
    expect(JSON.stringify(request)).not.toContain("eyJsecret");
    expect(response).toEqual({
      status: 200,
      sent: { mode: "forced", model: "m", tool_choice: { type: "function", name: "shell" } },
      model: "m",
      tool_choice: "auto",
      usage: { input_tokens: 9 },
      output: [{ type: "function_call", name: "shell" }],
    });
  });

  it("summarizes a stream the client cut short as unparsed rather than throwing", () => {
    expect(summarizeResponse('event: response.created\ndata: {"type":"response.cre')).toHaveProperty("unparsed");
  });
});

describe("forward", () => {
  it("ends the stream quietly when the client hangs up, as Codex does after every turn", async () => {
    const client = new AbortController();
    const upstream = (async (_url: RequestInfo | URL, init?: RequestInit) =>
      new Response(
        new ReadableStream<Uint8Array>({
          start: (controller) => controller.enqueue(new TextEncoder().encode("data: done\n\n")),
          // Like fetch: an aborted request errors the body it was streaming.
          pull: () => new Promise((_resolve, reject) => init!.signal!.addEventListener("abort", () => reject(init!.signal!.reason))),
        }),
      )) as typeof fetch;
    const incoming = new Request("http://router.test/v1/responses", { method: "POST", body: "{}", signal: client.signal });
    const reader = (await forward(incoming, testConfig(), upstream, { body: "{}" })).body!.getReader();

    expect(new TextDecoder().decode((await reader.read()).value)).toBe("data: done\n\n");
    const next = reader.read();
    client.abort();
    await expect(next).resolves.toEqual({ done: true, value: undefined });
  });
});
