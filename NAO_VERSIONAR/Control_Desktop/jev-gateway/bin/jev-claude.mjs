#!/usr/bin/env node
// jev-claude: run Claude Code through a local jev-gateway. Nothing in ~/.claude is modified.
import { claude } from "./clients.mjs";
import { runLauncher } from "./launcher.mjs";

await runLauncher(claude);
