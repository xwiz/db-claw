/**
 * Vocabulary sanitiser — runs at extraction time.
 *
 * Mirrors `python/semsql_rewriter/src/semsql_rewriter/sanitiser.py` and
 * `crates/semsql-core/src/ids.rs`. All three implementations must apply the
 * same rule:
 *
 *     canonical names match: [A-Za-z_][A-Za-z0-9_]{0,63}
 *
 * If you change the rule, change all three.
 */

const CANONICAL_RE = /^[A-Za-z_][A-Za-z0-9_]{0,63}$/;
const MAX_LABEL_LEN = 256;
const ZERO_WIDTH = new Set(["​", "‌", "‍", "⁠", "﻿"]);

/** Thrown when a vocabulary fragment fails sanitisation. */
export class SanitiserError extends Error {
	constructor(message: string) {
		super(message);
		this.name = "SanitiserError";
	}
}

/** Validate a canonical name (will be quoted as a SQL identifier downstream). */
export function sanitiseCanonical(raw: unknown): string {
	if (typeof raw !== "string") {
		throw new SanitiserError(`canonical must be string, got ${typeof raw}`);
	}
	if (!CANONICAL_RE.test(raw)) {
		throw new SanitiserError(`invalid canonical name: ${JSON.stringify(raw)}`);
	}
	return raw;
}

/** Normalise a free-text display label — never reaches SQL, only the vocabulary index. */
export function sanitiseLabel(raw: unknown): string {
	if (typeof raw !== "string") {
		throw new SanitiserError(`label must be string, got ${typeof raw}`);
	}
	let cleaned = raw.normalize("NFC");
	cleaned = Array.from(cleaned)
		.filter((ch) => !ZERO_WIDTH.has(ch) && !isControl(ch))
		.join("")
		.trim();
	if (!cleaned) {
		throw new SanitiserError("empty label after sanitisation");
	}
	if (cleaned.length > MAX_LABEL_LEN) {
		throw new SanitiserError(
			`label exceeds ${MAX_LABEL_LEN} chars: ${JSON.stringify(cleaned.slice(0, 32))}…`,
		);
	}
	return cleaned;
}

function isControl(ch: string): boolean {
	const code = ch.codePointAt(0) ?? 0;
	return code < 0x20 || (code >= 0x7f && code <= 0x9f);
}
