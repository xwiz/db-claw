export function normalizeText(input: string): string {
	return input
		.normalize("NFKD")
		.replace(/\p{M}/gu, "")
		.toLowerCase()
		.replace(/&/g, " and ")
		.replace(/[^a-z0-9]+/g, " ")
		.trim()
		.replace(/\s+/g, " ");
}

export function slugId(label: string, fallback: string): string {
	const base = normalizeText(label).replace(/\s+/g, "_");
	const cleaned = base.replace(/^[^a-z_]+/, "").replace(/[^a-z0-9_]/g, "");
	return cleaned.length > 0 ? cleaned : fallback;
}

export function dedupeIds(ids: string[]): string[] {
	const seen = new Map<string, number>();
	return ids.map((id) => {
		const count = seen.get(id) ?? 0;
		seen.set(id, count + 1);
		return count === 0 ? id : `${id}_${count + 1}`;
	});
}

export function hasPhrase(haystack: string, phrase: string): boolean {
	const normalizedPhrase = normalizeText(phrase);
	if (normalizedPhrase.length === 0) return false;
	return ` ${haystack} `.includes(` ${normalizedPhrase} `);
}

export function singularize(word: string): string {
	if (word.endsWith("ies") && word.length > 3) {
		return `${word.slice(0, -3)}y`;
	}
	if (word.endsWith("s") && word.length > 3) {
		return word.slice(0, -1);
	}
	return word;
}

export function words(input: string): string[] {
	const normalized = normalizeText(input);
	return normalized.length === 0 ? [] : normalized.split(" ");
}
