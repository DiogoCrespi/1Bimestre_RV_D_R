import type { Questions, SystemOneRequest, SystemOneResult } from "@typesafe-ai/sdk";
import { loadConfig, type Config } from "../src/config.js";
import type { AskJev } from "../src/decide.js";
import type { ToolDef } from "../src/types.js";

export const testConfig = (overrides: Partial<Config> = {}): Config => ({
  ...loadConfig({ UPSTREAM_BASE_URL: "https://llm.test/v1" }),
  ...overrides,
});

type Canned = Record<string, { choice: string; confidence?: number } | { noul: number }>;

/** A Jev stand-in: answers from `canned`, and records every request it was sent. */
export function fakeJev(canned: Canned) {
  const requests: SystemOneRequest<Questions>[] = [];
  const askJev: AskJev = async (request) => {
    requests.push(request);
    const answers: Record<string, unknown> = {};
    for (const [key, question] of Object.entries(request.questions)) {
      const answer = canned[key];
      if (!answer) throw new Error(`fakeJev has no canned answer for "${key}"`);
      if ("noul" in answer) {
        answers[key] = { type: "noul", noul: answer.noul };
      } else {
        if (question.type !== "choice" || !(answer.choice in question.criteria)) {
          throw new Error(`"${answer.choice}" is not an option of "${key}"`);
        }
        answers[key] = {
          type: "choice",
          choice: answer.choice,
          confidence: answer.confidence ?? 0.95,
          probabilities: { [answer.choice]: 1 },
        };
      }
    }
    return { model: "jev-test", answers, usage: { input_tokens: 123, output_tokens: 0 } } as SystemOneResult<Questions>;
  };
  return { askJev, requests };
}

/** An upstream stand-in that records requests and echoes a fixed completion. */
export function fakeUpstream(reply: unknown = { id: "chatcmpl-upstream", choices: [] }) {
  const calls: { url: string; headers: Headers; body: any }[] = [];
  const fetchImpl = (async (input: RequestInfo | URL, init?: RequestInit) => {
    const raw = init?.body;
    const text = typeof raw === "string" ? raw : raw instanceof Uint8Array ? Buffer.from(raw).toString("utf8") : undefined;
    calls.push({ url: String(input), headers: new Headers(init?.headers), body: text ? JSON.parse(text) : undefined });
    return Response.json(reply);
  }) as typeof fetch;
  return { fetchImpl, calls };
}

export const tools: ToolDef[] = [
  {
    type: "function",
    function: {
      name: "get_weather",
      description: "Get the current weather for a city.",
      parameters: { type: "object", properties: { city: { type: "string" } }, required: ["city"] },
    },
  },
  {
    type: "function",
    function: {
      name: "set_lights",
      description: "Turn the lights in a room on or off.",
      parameters: {
        type: "object",
        properties: {
          room: { type: "string", enum: ["kitchen", "bedroom", "office"] },
          on: { type: "boolean" },
          brightness: { type: "integer", enum: [25, 50, 100], description: "Brightness percent" },
        },
        required: ["room", "on"],
      },
    },
  },
];

export const chat = (content: string, extra: Record<string, unknown> = {}) => ({
  model: "gpt-test",
  messages: [{ role: "user", content }],
  tools,
  ...extra,
});

/** Requests are logged once their reply has been read to the end, in the background: let that finish. */
export const settled = () => new Promise((resolve) => setTimeout(resolve, 5));
