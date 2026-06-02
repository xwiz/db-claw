import {
	SHEET_USE_CASES,
	type SheetDataset,
	type SheetQueryResult,
	type SheetUseCase,
	buildSheetDataset,
	loadCsvFromUrl,
	parseCsv,
	querySheet,
	suggestSheetQuestions,
} from "@semsql/sheets";

interface DemoState {
	useCase: SheetUseCase;
	dataset: SheetDataset;
	sourceLabel: string;
	description: string;
	questions: string[];
}

const els = {
	status: required<HTMLDivElement>("status"),
	useCase: required<HTMLSelectElement>("use-case"),
	description: required<HTMLParagraphElement>("use-case-description"),
	url: required<HTMLInputElement>("sheet-url"),
	loadUrl: required<HTMLButtonElement>("load-url"),
	file: required<HTMLInputElement>("csv-file"),
	reset: required<HTMLButtonElement>("reload-use-case"),
	question: required<HTMLInputElement>("question"),
	run: required<HTMLButtonElement>("run"),
	quick: required<HTMLDivElement>("quick"),
	preview: required<HTMLDivElement>("preview"),
	schema: required<HTMLDivElement>("schema"),
	message: required<HTMLDivElement>("message"),
	shape: required<HTMLDivElement>("shape"),
	chart: required<HTMLDivElement>("chart"),
	table: required<HTMLDivElement>("result-table"),
	debug: required<HTMLPreElement>("debug"),
};

let state = loadUseCase(SHEET_USE_CASES[0]!);

function required<T extends HTMLElement>(id: string): T {
	const element = document.getElementById(id);
	if (!element) throw new Error(`missing #${id}`);
	return element as T;
}

function html(value: unknown): string {
	return String(value ?? "")
		.replace(/&/g, "&amp;")
		.replace(/</g, "&lt;")
		.replace(/>/g, "&gt;")
		.replace(/"/g, "&quot;");
}

function loadUseCase(useCase: SheetUseCase): DemoState {
	return {
		useCase,
		dataset: buildSheetDataset(parseCsv(useCase.csv)),
		sourceLabel: useCase.name,
		description: useCase.description,
		questions: useCase.questions,
	};
}

function setStatus(text: string): void {
	els.status.textContent = text;
}

function renderUseCases(): void {
	els.useCase.innerHTML = SHEET_USE_CASES.map(
		(useCase) =>
			`<option value="${html(useCase.id)}">${html(useCase.name)}</option>`,
	).join("");
	els.useCase.value = state.useCase.id;
	renderUseCaseDetails();
}

function renderUseCaseDetails(): void {
	els.description.textContent = state.description;
	els.quick.innerHTML = "";
	for (const question of state.questions) {
		const button = document.createElement("button");
		button.type = "button";
		button.textContent = question;
		button.addEventListener("click", () => runQuery(question));
		els.quick.append(button);
	}
	els.question.value = state.questions[0] ?? "";
}

function renderTable(
	rows: Record<string, string | number | boolean | null>[],
	target: HTMLDivElement,
): void {
	if (rows.length === 0) {
		target.innerHTML =
			"<table><tbody><tr><td>No rows</td></tr></tbody></table>";
		return;
	}
	const headers = Object.keys(rows[0]!);
	target.innerHTML = `<table><thead><tr>${headers
		.map((header) => `<th>${html(header)}</th>`)
		.join("")}</tr></thead><tbody>${rows
		.map(
			(row) =>
				`<tr>${headers
					.map((header) => `<td>${html(row[header])}</td>`)
					.join("")}</tr>`,
		)
		.join("")}</tbody></table>`;
}

function renderPreview(): void {
	const rows = state.dataset.rows.slice(0, 6).map((row) => {
		const out: Record<string, string | number | boolean | null> = {};
		for (const column of state.dataset.columns.slice(0, 5)) {
			out[column.label] = row.cells[column.id]?.raw ?? "";
		}
		return out;
	});
	renderTable(rows, els.preview);
	setStatus(
		`${state.sourceLabel}: ${state.dataset.rowCount} rows, ${state.dataset.columns.length} columns`,
	);
	renderSchema();
}

function renderSchema(): void {
	els.schema.innerHTML = state.dataset.columns
		.map((column) => {
			const examples = column.examples.slice(0, 3).join(", ");
			return `<div class="schema-item"><div class="schema-name">${html(column.label)}</div><div class="schema-meta">${html(column.kind)} - ${html(column.roles.join(", "))}</div><div class="schema-examples">${html(examples || "No examples")}</div></div>`;
		})
		.join("");
}

function confidenceText(result: SheetQueryResult): string {
	const confidence = result.confidence;
	if (!confidence) return "rejected";
	return `${confidence.level} confidence ${Math.round(confidence.score * 100)}%`;
}

function renderChart(result: SheetQueryResult): void {
	if (!result.ok || !result.chart || result.chart.values.length === 0) {
		els.chart.innerHTML = "";
		return;
	}
	const max = Math.max(...result.chart.values, 1);
	els.chart.innerHTML = `<div class="chart">${result.chart.labels
		.map((label, idx) => {
			const value = result.chart?.values[idx] ?? 0;
			const width = Math.max(4, (value / max) * 100);
			return `<div class="bar-row"><div>${html(label)}</div><div class="bar-track"><div class="bar" style="width:${width}%"></div></div><div>${html(value)}</div></div>`;
		})
		.join("")}</div>`;
}

function renderResult(result: SheetQueryResult): void {
	els.debug.textContent = JSON.stringify(
		result.ok ? result.queryFrame : result,
		null,
		2,
	);
	els.message.classList.toggle("ok", result.ok);
	els.message.classList.toggle("bad", !result.ok);
	if (!result.ok) {
		els.message.textContent = result.rejectionReason;
		els.shape.textContent = confidenceText(result);
		els.chart.innerHTML = "";
		els.table.innerHTML = "";
		return;
	}
	els.message.textContent = result.message;
	els.shape.textContent = `${result.queryFrame.resultShape} | ${confidenceText(result)}`;
	renderChart(result);
	renderTable(result.rows, els.table);
}

function runQuery(question = els.question.value): void {
	els.question.value = question;
	renderResult(querySheet(state.dataset, question));
}

function replaceDataset(csv: string, sourceLabel: string): void {
	const dataset = buildSheetDataset(parseCsv(csv));
	const questions = suggestSheetQuestions(dataset);
	state = {
		...state,
		dataset,
		sourceLabel,
		description: "Loaded CSV",
		questions:
			questions.length > 0 ? questions : ["show rows", "how many rows?"],
	};
	renderUseCaseDetails();
	renderPreview();
	runQuery();
}

function selectUseCase(id: string): void {
	const useCase =
		SHEET_USE_CASES.find((candidate) => candidate.id === id) ??
		SHEET_USE_CASES[0]!;
	state = loadUseCase(useCase);
	renderUseCaseDetails();
	renderPreview();
	runQuery();
}

async function loadUrl(): Promise<void> {
	const url = els.url.value.trim();
	if (!url) return;
	setStatus("Loading CSV");
	try {
		const dataset = buildSheetDataset(await loadCsvFromUrl(url));
		const questions = suggestSheetQuestions(dataset);
		state = {
			...state,
			dataset,
			sourceLabel: "Loaded CSV",
			description: "Loaded public CSV",
			questions:
				questions.length > 0 ? questions : ["show rows", "how many rows?"],
		};
		renderUseCaseDetails();
		renderPreview();
		runQuery();
	} catch (error) {
		els.message.textContent =
			error instanceof Error ? error.message : String(error);
		els.message.classList.remove("ok");
		els.message.classList.add("bad");
		setStatus("Load failed");
	}
}

async function loadFile(): Promise<void> {
	const file = els.file.files?.[0];
	if (!file) return;
	replaceDataset(await file.text(), file.name);
}

function bindEvents(): void {
	els.useCase.addEventListener("change", () =>
		selectUseCase(els.useCase.value),
	);
	els.reset.addEventListener("click", () => selectUseCase(state.useCase.id));
	els.loadUrl.addEventListener("click", () => void loadUrl());
	els.file.addEventListener("change", () => void loadFile());
	els.run.addEventListener("click", () => runQuery());
	els.question.addEventListener("keydown", (event) => {
		if (event.key === "Enter") runQuery();
	});
}

renderUseCases();
renderPreview();
bindEvents();
runQuery();
