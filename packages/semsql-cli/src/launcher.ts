#!/usr/bin/env node
import { spawn } from "node:child_process";

import { ensureSemsqlBinary } from "./downloader.js";

try {
	const binary = await ensureSemsqlBinary();
	const child = spawn(binary, process.argv.slice(2), {
		stdio: "inherit",
		windowsHide: true,
	});
	child.on("error", (error) => {
		console.error(`failed to launch semsql: ${error.message}`);
		process.exit(1);
	});
	child.on("exit", (code, signal) => {
		if (signal) {
			process.kill(process.pid, signal);
			return;
		}
		process.exit(code ?? 0);
	});
} catch (error) {
	const message = error instanceof Error ? error.message : String(error);
	console.error(message);
	process.exit(1);
}
