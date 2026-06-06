/**
 * Django REST Framework serializers walker.
 *
 * Walks every `**\/serializers.py` file and emits ApiResource-layer
 * (=4) vocabulary fragments for serializer-defined labels. The label
 * surface is small but high-fidelity: DRF lifts `label=` into
 * generated OpenAPI schemas and into the browsable API, so users see
 * those exact strings in real product surfaces.
 *
 * Recognised patterns (v0.6 cut):
 *
 *   - `class FooSerializer(serializers.ModelSerializer)` —
 *     `class Meta: model = Foo` resolves the underlying entity
 *     (snake_case of the bare identifier — `User` → `user`).
 *   - `field = serializers.X(label='Display', source='field_on_model')`
 *     emits an ApiResource-layer label fragment for `entity.<source>`
 *     (or `entity.<field>` when no `source=` is supplied).
 *   - `help_text='...'` on a serializer field also emits at the
 *     ApiResource layer (same rationale as the models walker).
 *
 * Out of scope for v0.6:
 *
 *   - `Meta.fields = ['…']` is parsed only to validate the serializer
 *     declares a real model — we don't promote bare names to labels
 *     because they're field identifiers, not vocabulary.
 *   - Cross-app entity resolution (`model = some_app.User`).
 *   - SerializerMethodField — these are computed, not vocabulary.
 *   - Nested serializers — emitted independently when the inner
 *     serializer ships its own `class Meta`.
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

/** Result of walking one project's serializers.py files. */
export interface DjangoSerializersScanResult {
	fragments: VocabFragment[];
	/** Files we recognised but couldn't parse. */
	skipped: Array<{ file: string; reason: string }>;
}

export async function scanDjangoSerializers(
	root: string,
): Promise<DjangoSerializersScanResult> {
	const result: DjangoSerializersScanResult = { fragments: [], skipped: [] };
	for await (const file of findSerializerFiles(root)) {
		await scanFile(file, result);
	}
	return result;
}

async function* findSerializerFiles(dir: string): AsyncGenerator<string> {
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
			yield* findSerializerFiles(full);
		} else if (entry === "serializers.py") {
			yield full;
		}
	}
}

async function scanFile(
	file: string,
	result: DjangoSerializersScanResult,
): Promise<void> {
	const text = await fs.readFile(file, "utf8");
	const lazy = getParser();
	if (!lazy) {
		result.skipped.push({
			file,
			reason:
				"tree-sitter-python native binding unavailable — rebuild on this CPU",
		});
		return;
	}
	const tree = lazy.parser.parse(text);
	walk(tree.rootNode, (node) => {
		if (node.type !== "class_definition") return;
		if (!isDrfSerializer(node)) return;
		emitFromSerializerClass(file, node, result);
		return true;
	});
}

/**
 * True iff the class subclasses a recognisable DRF serializer base.
 * Accepts the canonical idioms — `serializers.ModelSerializer`,
 * `serializers.Serializer`, bare `ModelSerializer` / `Serializer`,
 * and any user-defined class whose name ends in `Serializer` (this
 * latter catches in-project base classes like
 * `class TimestampedSerializer(serializers.ModelSerializer)`).
 */
function isDrfSerializer(classNode: SyntaxNode): boolean {
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
			text === "serializers.ModelSerializer" ||
			text === "serializers.Serializer" ||
			text === "serializers.HyperlinkedModelSerializer" ||
			text === "ModelSerializer" ||
			text === "Serializer" ||
			text === "HyperlinkedModelSerializer" ||
			text.endsWith("Serializer")
		) {
			return true;
		}
	}
	return false;
}

function emitFromSerializerClass(
	file: string,
	classNode: SyntaxNode,
	result: DjangoSerializersScanResult,
): void {
	const body = classNode.childForFieldName("body");
	if (!body) return;

	const entity = readSerializerEntity(body);
	if (entity === null) return;

	let canonicalEntity: string;
	try {
		canonicalEntity = sanitiseCanonical(entity);
	} catch (e) {
		if (e instanceof SanitiserError) {
			result.skipped.push({ file, reason: e.message });
			return;
		}
		throw e;
	}

	for (let i = 0; i < body.namedChildCount; i++) {
		const stmt = body.namedChild(i);
		if (!stmt || stmt.type !== "expression_statement") continue;
		const inner = stmt.namedChild(0);
		if (!inner || inner.type !== "assignment") continue;
		emitFromSerializerField(file, canonicalEntity, inner, result);
	}
}

/**
 * Resolve the entity bound to this serializer via `class Meta:
 * model = Foo`. Returns the snake_case form of the model class
 * name, or `null` if no Meta block / no model assignment.
 *
 * `Meta.model = some_app.User` (dotted) is reduced to the tail
 * segment — full cross-app resolution is a follow-up.
 */
function readSerializerEntity(classBody: SyntaxNode): string | null {
	for (let i = 0; i < classBody.namedChildCount; i++) {
		const stmt = classBody.namedChild(i);
		if (!stmt || stmt.type !== "class_definition") continue;
		const name = stmt.childForFieldName("name");
		if (!name || name.text !== "Meta") continue;
		const inner = stmt.childForFieldName("body");
		if (!inner) continue;
		for (let j = 0; j < inner.namedChildCount; j++) {
			const s = inner.namedChild(j);
			if (!s || s.type !== "expression_statement") continue;
			const a = s.namedChild(0);
			if (!a || a.type !== "assignment") continue;
			const lhs = a.childForFieldName("left");
			const rhs = a.childForFieldName("right");
			if (!lhs || !rhs) continue;
			if (lhs.type !== "identifier" || lhs.text !== "model") continue;
			const tail = lastIdentifierSegment(rhs);
			if (tail !== null) return toSnakeCase(tail);
		}
	}
	return null;
}

function lastIdentifierSegment(node: SyntaxNode): string | null {
	if (node.type === "identifier") return node.text;
	if (node.type === "attribute") {
		// Walk down `.attribute` chain — last segment is the class.
		const attrName = node.childForFieldName("attribute");
		if (attrName) return attrName.text;
		return node.text.split(".").slice(-1)[0] ?? null;
	}
	return null;
}

function emitFromSerializerField(
	file: string,
	canonicalEntity: string,
	assign: SyntaxNode,
	result: DjangoSerializersScanResult,
): void {
	const lhs = assign.childForFieldName("left");
	const rhs = assign.childForFieldName("right");
	if (!lhs || !rhs) return;
	if (lhs.type !== "identifier") return;
	if (rhs.type !== "call") return;
	const callee = rhs.childForFieldName("function");
	if (!callee) return;
	const calleeText = callee.text.trim();
	// Accept `serializers.X(...)` and any bare `XField`/`XSerializer`.
	const isSerializerCall =
		calleeText.startsWith("serializers.") ||
		/^[A-Z][A-Za-z0-9]*Field$/.test(calleeText) ||
		/^[A-Z][A-Za-z0-9]*Serializer$/.test(calleeText);
	if (!isSerializerCall) return;

	const args = rhs.childForFieldName("arguments");
	if (!args) return;

	const label = readKwargString(args, "label");
	const helpText = readKwargString(args, "help_text");
	const source = readKwargString(args, "source");

	if (label === null && helpText === null) return;

	const fieldName = source ?? lhs.text;
	let canonicalField: string;
	try {
		canonicalField = sanitiseCanonical(fieldName);
	} catch (e) {
		if (e instanceof SanitiserError) {
			result.skipped.push({ file, reason: e.message });
			return;
		}
		throw e;
	}
	const line = (assign.startPosition.row ?? 0) + 1;

	if (label !== null) {
		emitFieldLabel(file, canonicalEntity, canonicalField, label, line, result);
	}
	if (helpText !== null) {
		emitFieldLabel(
			file,
			canonicalEntity,
			canonicalField,
			helpText,
			line,
			result,
		);
	}
}

function emitFieldLabel(
	file: string,
	canonicalEntity: string,
	canonicalField: string,
	rawLabel: string,
	line: number,
	result: DjangoSerializersScanResult,
): void {
	let label: string;
	try {
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
			extractor: "extractor-django:serializers",
		},
	});
}

function readKwargString(args: SyntaxNode, name: string): string | null {
	for (let i = 0; i < args.namedChildCount; i++) {
		const c = args.namedChild(i);
		if (!c || c.type !== "keyword_argument") continue;
		const kwName = c.childForFieldName("name");
		const kwVal = c.childForFieldName("value");
		if (!kwName || !kwVal) continue;
		if (kwName.text !== name) continue;
		if (kwVal.type !== "string") return null;
		return parsePythonStringLiteral(kwVal);
	}
	return null;
}
