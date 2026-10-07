#!/usr/bin/env node
// jev-codex: run Codex through a local jev-gateway. Nothing in ~/.codex is modified.
import { codex } from "./clients.mjs";
import { runLauncher } from "./launcher.mjs";

await runLauncher(codex);
