import { parseCsv } from "./csv.js";
import type { CsvData } from "./types.js";

export interface LoadCsvOptions {
	fetch?: typeof fetch;
}

function gidFromHash(hash: string): string | undefined {
	const match = hash.match(/gid=([0-9]+)/);
	return match?.[1];
}

export function toGoogleSheetsCsvUrl(input: string): string {
	const trimmed = input.trim();
	const url = new URL(trimmed);
	if (!url.hostname.includes("docs.google.com")) {
		return trimmed;
	}
	const match = url.pathname.match(/\/spreadsheets\/d\/([^/]+)/);
	if (!match) {
		return trimmed;
	}

	if (url.pathname.endsWith("/export")) {
		url.searchParams.set("format", "csv");
		return url.toString();
	}

	if (url.pathname.endsWith("/pub")) {
		url.searchParams.set("output", "csv");
		return url.toString();
	}

	const id = match[1]!;
	const gid = url.searchParams.get("gid") ?? gidFromHash(url.hash) ?? "0";
	const out = new URL(`https://docs.google.com/spreadsheets/d/${id}/export`);
	out.searchParams.set("format", "csv");
	out.searchParams.set("gid", gid);
	return out.toString();
}

export async function loadCsvFromUrl(
	input: string,
	options: LoadCsvOptions = {},
): Promise<CsvData> {
	const fetcher = options.fetch ?? globalThis.fetch;
	if (!fetcher) {
		throw new Error("fetch is not available in this environment");
	}
	const url = toGoogleSheetsCsvUrl(input);
	const response = await fetcher(url);
	if (!response.ok) {
		throw new Error(
			`CSV fetch failed: ${response.status} ${response.statusText}`,
		);
	}
	return parseCsv(await response.text());
}
