#!/usr/bin/env node
// jev-gemini: run Gemini clients through a local jev-gateway.
import { gemini } from "./clients.mjs";
import { runLauncher } from "./launcher.mjs";

await runLauncher(gemini);
