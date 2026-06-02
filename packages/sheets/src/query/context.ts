import { normalizeText, singularize, words } from "../normalize.js";

export interface RouteContext {
	raw: string;
	normalized: string;
	questionWords: string[];
	singularQuestionWords: string[];
}

export function contextForPhrase(phrase: string): RouteContext {
	return {
		raw: phrase,
		normalized: normalizeText(phrase),
		questionWords: words(phrase),
		singularQuestionWords: words(phrase).map((word) => singularize(word)),
	};
}
