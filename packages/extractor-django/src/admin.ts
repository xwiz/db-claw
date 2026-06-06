/**
 * Django admin walker — `admin.py` `ModelAdmin` subclasses.
 *
 * The Django admin surfaces a small but high-fidelity vocabulary
 * surface: `short_description` (legacy) and `@admin.display(description=...)`
 * (Django 3.2+). Both name a column header that staff users see in
 * the admin list view — high-priority enough to flag at the
 * `ApiResource` layer (=4), one above ORM-defined `verbose_name`.
 *
 * Recognised entity-binding patterns:
 *
 *   1. `@admin.register(Foo)` decorator on the admin class — most
 *      common since Django 1.7.
 *   2. `admin.site.register(Foo, FooAdmin)` module-level call —
 *      legacy idiom, still in older codebases.
 *
 * Deliberately not promoted by this admin walker:
 *
 *   - `class FooInline(admin.TabularInline)` emits if `model = ...`
 *     present, but inline-specific labels (`verbose_name_plural`) are
 *     left to the model `Meta` reader, which is more authoritative.
 *   - `list_display`/`fieldsets` field references are NOT promoted to
 *     vocabulary on their own. They're field identifiers, not labels.
 *   - Cross-file resolution: `@admin.register` has to bind to a class
 *     name visible in the same file. This matches the conventional
 *     Django app layout.
 */

import { promises as fs } from "node:fs";
import path from "node:path";
import {
	SanitiserError,
	SourceLayer,
	type VocabFragment,
	sanitiseCanonical,
	sanitiseLabel,
} from "@semsql/extractor-sdk";

import { toSnakeCase } from "./models.js";
import {
	type SyntaxNode,
	getParser,
	parsePythonStringLiteral,
	walk,
} from "./python-ast.js";

/** Result of walking one project's admin.py files. */
export interface DjangoAdminScanResult {
	fragments: VocabFragment[];
	/** Files we recognised but couldn't parse. */
	skipped: Array<{ file: string; reason: string }>;
}

export async function scanDjangoAdmin(
	root: string,
): Promise<DjangoAdminScanResult> {
	const result: DjangoAdminScanResult = { fragments: [], skipped: [] };
	const lazy = getParser();
	if (!lazy) {
		result.skipped.push({
			file: root,
			reason:
				"tree-sitter-python native binding unavailable — rebuild on this CPU",
		});
		return result;
	}
	for await (const file of findAdminFiles(root)) {
		const text = await fs.readFile(file, "utf8");
		const tree = lazy.parser.parse(text);
		scanTree(file, tree.rootNode, result);
	}
	return result;
}

async function* findAdminFiles(dir: string): AsyncGenerator<string> {
	let entries: string[];
	try {
		entries = await fs.readdir(dir);
	} catch {
		return;
	}
	for (const entry of entries) {
		const full = path.join(dir, entry);
		const stat = await fs.stat(full).catch(() => null);
		if (!stat) continue;
		if (stat.isDirectory()) {
			if (
				entry === "node_modules" ||
				entry === ".venv" ||
				entry === "venv" ||
				entry === "__pycache__" ||
				entry === ".git" ||
				entry === "migrations"
			) {
				continue;
			}
			yield* findAdminFiles(full);
		} else if (entry === "admin.py") {
			yield full;
		}
	}
}

interface AdminClassMatch {
	classNode: SyntaxNode;
	className: string;
	/** Entity bound via `@admin.register(Foo)` decorator. */
	decoratorEntity: string | null;
}

/**
 * Walk a parsed admin.py tree and emit fragments. Two passes:
 *
 *   1. Collect every ModelAdmin subclass with its register-decorator
 *      entity binding (when present).
 *   2. Walk the module top-level for `admin.site.register(Foo,
 *      FooAdmin)` calls — these patch the entity binding for any
 *      ModelAdmin missed by phase 1.
 *   3. Emit fragments for every bound admin class.
 */
function scanTree(
	file: string,
	root: SyntaxNode,
	result: DjangoAdminScanResult,
): void {
	const admins: AdminClassMatch[] = [];

	// Phase 1 — collect every class_definition matching a ModelAdmin.
	walk(root, (node) => {
		if (node.type === "decorated_definition") {
			const inner = node.namedChildren.find(
				(c) => c.type === "class_definition",
			);
			if (!inner) return;
			if (!isAdminClass(inner)) return;
			const className = inner.childForFieldName("name")?.text;
			if (!className) return;
			admins.push({
				classNode: inner,
				className,
				decoratorEntity: findRegisterDecoratorEntity(node),
			});
			return true;
		}
		if (node.type === "class_definition") {
			if (!isAdminClass(node)) return;
			const className = node.childForFieldName("name")?.text;
			if (!className) return;
			admins.push({ classNode: node, className, decoratorEntity: null });
			return true;
		}
		return;
	});

	// Phase 2 — patch entity bindings via `admin.site.register(Foo,
	// FooAdmin)` calls. Only fills classes that lack a decorator.
	const siteBindings = collectSiteRegisterBindings(root);

	// Phase 3 — emit fragments for every admin class with a known
	// entity. Admins with no binding are silently skipped — without
	// an entity we can't map labels to canonical fields.
	for (const adm of admins) {
		const entityRaw = adm.decoratorEntity ?? siteBindings.get(adm.className);
		if (!entityRaw) continue;

		let canonicalEntity: string;
		try {
			canonicalEntity = sanitiseCanonical(entityRaw);
		} catch (e) {
			if (e instanceof SanitiserError) {
				result.skipped.push({ file, reason: e.message });
				continue;
			}
			throw e;
		}

		emitAdminLabels(file, canonicalEntity, adm.classNode, result);
	}
}

/**
 * True iff `class_definition` directly subclasses a recognised admin
 * base — `admin.ModelAdmin`, `ModelAdmin`, plus inline variants. Like
 * the model walker, we accept user-defined `*Admin`-suffixed bases
 * (project-local mixins).
 */
function isAdminClass(classNode: SyntaxNode): boolean {
	const supers = classNode.childForFieldName("superclasses");
	if (!supers) return false;
	for (let i = 0; i < supers.namedChildCount; i++) {
		const base = supers.namedChild(i);
		if (!base) continue;
		const inner =
			base.type === "argument" && base.namedChildCount > 0
				? base.namedChild(0)!
				: base;
		const text = inner.text.trim();
		if (
			text === "admin.ModelAdmin" ||
			text === "ModelAdmin" ||
			text === "admin.TabularInline" ||
			text === "TabularInline" ||
			text === "admin.StackedInline" ||
			text === "StackedInline" ||
			text.endsWith("Admin")
		) {
			return true;
		}
	}
	return false;
}

/**
 * Pull the entity from `@admin.register(Foo)` (or the bare
 * `@register(Foo)` shorthand). Multiple model arguments — the first
 * is used; mixed-model registrations are unusual and the merge
 * engine handles cross-decorator collisions downstream.
 */
function findRegisterDecoratorEntity(decoratedDef: SyntaxNode): string | null {
	for (let i = 0; i < decoratedDef.namedChildCount; i++) {
		const c = decoratedDef.namedChild(i);
		if (!c || c.type !== "decorator") continue;
		// Decorator AST: `decorator { call { attribute_or_id, argument_list } }`
		const inner = c.namedChild(0);
		if (!inner || inner.type !== "call") continue;
		const callee = inner.childForFieldName("function");
		if (!callee) continue;
		const calleeText = callee.text.trim();
		if (calleeText !== "admin.register" && calleeText !== "register") continue;
		const args = inner.childForFieldName("arguments");
		if (!args) continue;
		for (let j = 0; j < args.namedChildCount; j++) {
			const a = args.namedChild(j);
			if (!a) continue;
			const tail = lastIdentifierSegment(a);
			if (tail) return toSnakeCase(tail);
		}
	}
	return null;
}

/**
 * Walk the module top level for `admin.site.register(Foo, FooAdmin)`
 * calls and return a `FooAdmin → foo` map.
 */
function collectSiteRegisterBindings(root: SyntaxNode): Map<string, string> {
	const out = new Map<string, string>();
	for (let i = 0; i < root.namedChildCount; i++) {
		const stmt = root.namedChild(i);
		if (!stmt || stmt.type !== "expression_statement") continue;
		const callExpr = stmt.namedChild(0);
		if (!callExpr || callExpr.type !== "call") continue;
		const callee = callExpr.childForFieldName("function");
		if (!callee) continue;
		if (callee.text.trim() !== "admin.site.register") continue;
		const args = callExpr.childForFieldName("arguments");
		if (!args) continue;
		const positional: SyntaxNode[] = [];
		for (let j = 0; j < args.namedChildCount; j++) {
			const a = args.namedChild(j);
			if (!a) continue;
			if (a.type === "keyword_argument") break;
			positional.push(a);
		}
		if (positional.length < 2) continue;
		const modelTail = lastIdentifierSegment(positional[0]!);
		const adminClass = lastIdentifierSegment(positional[1]!);
		if (!modelTail || !adminClass) continue;
		if (!out.has(adminClass)) out.set(adminClass, toSnakeCase(modelTail));
	}
	return out;
}

function lastIdentifierSegment(node: SyntaxNode): string | null {
	if (node.type === "identifier") return node.text;
	if (node.type === "attribute") {
		const tail = node.childForFieldName("attribute");
		if (tail) return tail.text;
		return node.text.split(".").slice(-1)[0] ?? null;
	}
	return null;
}

/**
 * Walk an admin class body and emit ApiResource-layer label fragments
 * for every recognised vocabulary surface:
 *
 *   - `@admin.display(description='Display')` decorator on a
 *     `def method(...)` definition.
 *   - `<method>.short_description = 'Display'` legacy assignment.
 *   - `class Meta: verbose_name = 'Display'` inside an inline admin
 *     (TabularInline / StackedInline) — entity-level relabel.
 */
function emitAdminLabels(
	file: string,
	canonicalEntity: string,
	classNode: SyntaxNode,
	result: DjangoAdminScanResult,
): void {
	const body = classNode.childForFieldName("body");
	if (!body) return;

	// Pre-compute method labels for the legacy
	// `<method>.short_description = 'Display'` idiom. Walk first so
	// the assignment-style labels can be threaded back to the matching
	// method even when they appear later in the class body.
	const methodLabels = new Map<string, string>();
	const methodLabelLines = new Map<string, number>();

	for (let i = 0; i < body.namedChildCount; i++) {
		const stmt = body.namedChild(i);
		if (!stmt || stmt.type !== "expression_statement") continue;
		const a = stmt.namedChild(0);
		if (!a || a.type !== "assignment") continue;
		const lhs = a.childForFieldName("left");
		const rhs = a.childForFieldName("right");
		if (!lhs || !rhs) continue;
		if (lhs.type !== "attribute") continue;
		const owner = lhs.childForFieldName("object");
		const attrName = lhs.childForFieldName("attribute");
		if (!owner || !attrName) continue;
		if (owner.type !== "identifier") continue;
		if (attrName.text !== "short_description") continue;
		if (rhs.type !== "string") continue;
		const label = parsePythonStringLiteral(rhs);
		if (label === null) continue;
		methodLabels.set(owner.text, label);
		methodLabelLines.set(owner.text, (a.startPosition.row ?? 0) + 1);
	}

	// Walk function definitions — both bare and decorated. Two label
	// sources per method: the @admin.display decorator and the
	// legacy `.short_description` assignment. If both exist, the
	// decorator wins (it's the canonical idiom).
	const seenMethods = new Set<string>();
	for (let i = 0; i < body.namedChildCount; i++) {
		const stmt = body.namedChild(i);
		if (!stmt) continue;
		let funcDef: SyntaxNode | null = null;
		let decoratorDescription: { label: string; line: number } | null = null;

		if (stmt.type === "function_definition") {
			funcDef = stmt;
		} else if (stmt.type === "decorated_definition") {
			funcDef =
				stmt.namedChildren.find((c) => c.type === "function_definition") ??
				null;
			decoratorDescription = readAdminDisplayDecorator(stmt);
		} else {
			continue;
		}
		if (!funcDef) continue;
		const name = funcDef.childForFieldName("name")?.text;
		if (!name) continue;
		seenMethods.add(name);

		if (decoratorDescription) {
			emitFieldLabel(
				file,
				canonicalEntity,
				name,
				decoratorDescription.label,
				decoratorDescription.line,
				result,
			);
		} else if (methodLabels.has(name)) {
			emitFieldLabel(
				file,
				canonicalEntity,
				name,
				methodLabels.get(name)!,
				methodLabelLines.get(name)!,
				result,
			);
		}
	}

	// Defensive: emit any `<method>.short_description = ...` whose
	// matching `def method` lives outside the seen set (rare — a
	// module-level assignment after the class). Skipped today.
	for (const [name, label] of methodLabels) {
		if (seenMethods.has(name)) continue;
		// No matching method definition found in this class body —
		// emit anyway so the vocabulary still surfaces. The merge
		// engine downstream surfaces the dangling reference if no
		// matching field exists.
		emitFieldLabel(
			file,
			canonicalEntity,
			name,
			label,
			methodLabelLines.get(name) ?? 1,
			result,
		);
	}
}

/** Read `@admin.display(description="…")` (or `@display(description=…)`). */
function readAdminDisplayDecorator(
	decoratedDef: SyntaxNode,
): { label: string; line: number } | null {
	for (let i = 0; i < decoratedDef.namedChildCount; i++) {
		const c = decoratedDef.namedChild(i);
		if (!c || c.type !== "decorator") continue;
		const inner = c.namedChild(0);
		if (!inner || inner.type !== "call") continue;
		const callee = inner.childForFieldName("function");
		if (!callee) continue;
		const calleeText = callee.text.trim();
		if (calleeText !== "admin.display" && calleeText !== "display") continue;
		const args = inner.childForFieldName("arguments");
		if (!args) continue;
		for (let j = 0; j < args.namedChildCount; j++) {
			const a = args.namedChild(j);
			if (!a || a.type !== "keyword_argument") continue;
			const kwName = a.childForFieldName("name");
			const kwVal = a.childForFieldName("value");
			if (!kwName || !kwVal) continue;
			if (kwName.text !== "description") continue;
			if (kwVal.type !== "string") continue;
			const label = parsePythonStringLiteral(kwVal);
			if (label === null) continue;
			return { label, line: (c.startPosition.row ?? 0) + 1 };
		}
	}
	return null;
}

function emitFieldLabel(
	file: string,
	canonicalEntity: string,
	fieldName: string,
	rawLabel: string,
	line: number,
	result: DjangoAdminScanResult,
): void {
	let canonicalField: string;
	let label: string;
	try {
		canonicalField = sanitiseCanonical(fieldName);
		label = sanitiseLabel(rawLabel);
	} catch (e) {
		if (e instanceof SanitiserError) {
			result.skipped.push({ file, reason: e.message });
			return;
		}
		throw e;
	}
	result.fragments.push({
		term: label.toLowerCase(),
		canonical: { kind: "field", field: `${canonicalEntity}.${canonicalField}` },
		confidence: 0.85,
		locator: {
			file,
			line,
			layer: SourceLayer.ApiResource,
			extractor: "extractor-django:admin",
		},
	});
}
