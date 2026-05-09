/**
 * Django models walker.
 *
 * Walks every `**\/models.py` file in a project and emits vocabulary
 * fragments for:
 *
 *   - **Entities** — every `class Foo(models.Model)` (or any subclass
 *     of Django's `Model`). Canonical name is `snake_case(Foo)`,
 *     overridable by `class Meta: db_table = "foo_table"`.
 *   - **Field labels** — `name = models.CharField(verbose_name="...")`,
 *     `name = models.IntegerField("...", ...)` (positional `verbose_name`
 *     idiom), and any field passed `help_text=` (treated as ApiResource
 *     layer because help text is shown but not as a column header).
 *   - **Entity labels** — `class Meta: verbose_name = "Custom"` /
 *     `verbose_name_plural = "Customs"`.
 *
 * Layer assignment:
 *
 *   - Field `verbose_name` → `Orm` (=2) — these are ORM-defined labels,
 *     superseded by i18n / Filament-equivalent UI overrides.
 *   - `Meta.verbose_name` / `Meta.verbose_name_plural` → `Orm` too.
 *   - `help_text` → `ApiResource` (=4) — Django REST Framework lifts
 *     these into serializer descriptions; treating as ApiResource keeps
 *     them a level above raw ORM.
 *
 * Out of scope for this v0.5 cut:
 *
 *   - Multi-app namespacing (`<app>.<Model>` references — handled by a
 *     downstream merge step once the runtime supports namespaces).
 *   - `choices=[(...), ...]` enum extraction — coming in a follow-up.
 *   - Custom field classes that wrap `models.Field` — only direct
 *     `models.Foo(...)` patterns recognised. Most apps use these.
 *   - `class Meta: ordering = [...]`. Not vocabulary.
 *   - Abstract base classes — emitted regardless; the merge engine
 *     trims unreferenced entities later.
 */

import { promises as fs } from "node:fs";
import path from "node:path";
import {
    sanitiseCanonical,
    sanitiseLabel,
    SanitiserError,
    SourceLayer,
    type VocabFragment,
} from "@semsql/extractor-sdk";

import {
    getParser,
    parsePythonStringLiteral,
    type SyntaxNode,
    walk,
} from "./python-ast.js";

/** Result of walking one project's models.py files. */
export interface DjangoModelsScanResult {
    fragments: VocabFragment[];
    /** Files we recognised but couldn't parse (e.g. tree-sitter missing). */
    skipped: Array<{ file: string; reason: string }>;
}

/**
 * Walk every `models.py` file under `root` and emit vocabulary
 * fragments. The walker is opportunistic — non-Django Python files
 * named `models.py` (rare, but they exist in non-Django projects)
 * produce zero fragments because the recognition rules require a
 * `models.Model` subclass.
 */
export async function scanDjangoModels(root: string): Promise<DjangoModelsScanResult> {
    const result: DjangoModelsScanResult = { fragments: [], skipped: [] };

    // Three-phase orchestration:
    //
    //   Phase A — parse every models.py, build per-file ChoicesClass +
    //   ClassInfo registries plus a per-file imports map. Trees are
    //   cached so phase C doesn't re-parse.
    //
    //   Phase B — fold the per-file class indices into global maps
    //   keyed by qualified `<file>::<class_name>`. Bare-name lookup
    //   stays available as a back-compat fallback (most Django apps
    //   don't have cross-app collisions).
    //
    //   Phase C — walk each cached tree, emit fragments using:
    //     1. The file's own class registry (highest priority).
    //     2. The file's imports — `from .x import Status` resolves
    //        Status to the sibling `x.py`'s registry, eliminating the
    //        bare-name first-wins ambiguity.
    //     3. The global bare-name registry (fallback when neither (1)
    //        nor (2) match — covers idioms where a base is implicitly
    //        in scope via star imports or wildcard re-exports).
    //
    // The imports map is also used for *abstract base* resolution:
    // `class User(Timestamped):` looks up `Timestamped` first in the
    // file, then in imports, then globally.

    interface CachedFile {
        file: string;
        rootNode: SyntaxNode;
        localChoices: Map<string, ChoiceMember[]>;
        localClasses: Map<string, ClassInfo>;
        imports: Map<string, ImportTarget>;
    }
    const cached: CachedFile[] = [];

    const lazy = getParser();
    if (!lazy) {
        // Surface once per scan, not per file — every file would
        // produce the same noise otherwise.
        result.skipped.push({
            file: root,
            reason: "tree-sitter-python native binding unavailable — rebuild on this CPU",
        });
        return result;
    }

    // Phase A — parse + collect per-file.
    for await (const file of findModelsFiles(root)) {
        const text = await fs.readFile(file, "utf8");
        const tree = lazy.parser.parse(text);
        cached.push({
            file,
            rootNode: tree.rootNode,
            localChoices: collectChoicesClasses(tree.rootNode),
            localClasses: collectClassIndex(tree.rootNode),
            imports: collectImports(tree.rootNode),
        });
    }

    // Phase B — file-keyed registries for cross-file lookup, plus a
    // bare-name global fallback (first-wins; documented compromise).
    const choicesByFile = new Map<string, Map<string, ChoiceMember[]>>();
    const classByFile = new Map<string, Map<string, ClassInfo>>();
    const choicesGlobal = new Map<string, ChoiceMember[]>();
    const classGlobal = new Map<string, ClassInfo>();
    for (const c of cached) {
        choicesByFile.set(c.file, c.localChoices);
        classByFile.set(c.file, c.localClasses);
        for (const [name, members] of c.localChoices) {
            if (!choicesGlobal.has(name)) choicesGlobal.set(name, members);
        }
        for (const [name, info] of c.localClasses) {
            if (!classGlobal.has(name)) classGlobal.set(name, info);
        }
    }

    // Phase C — emit per file using import-aware resolvers.
    for (const c of cached) {
        const choicesResolver = makeChoicesResolver(
            c,
            choicesByFile,
            choicesGlobal,
        );
        const classResolver = makeClassResolver(c, classByFile, classGlobal);
        emitFromTree(c.file, c.rootNode, choicesResolver, classResolver, result);
    }

    return result;
}

/**
 * One import binding parsed from `from <module> import <name> [as <alias>]`.
 * `module` is normalised to a forward-slash path RELATIVE to the
 * importing file's directory — e.g. `.choices` → `choices`,
 * `..common.bases` → `../common/bases`. Absolute imports
 * (`apps.common.bases`) become `apps/common/bases` and resolve from
 * the project root supplied by the orchestrator.
 */
interface ImportTarget {
    /** Slash-form path, dropping the leading `./` for relative imports. */
    modulePath: string;
    /** True for `.x`, `..y` style imports — resolved against importer dir. */
    isRelative: boolean;
    /** How many dots prefixed the relative import (1 = sibling, 2 = parent). */
    relativeDepth: number;
    /** Original (pre-alias) symbol name. */
    originalName: string;
}

/**
 * Resolver: given a class name referenced in `c.file`, return its
 * `ChoiceMember[]` if findable. Lookup priority: (1) local, (2)
 * import-resolved, (3) global bare-name fallback.
 */
type ChoicesResolver = (name: string) => ChoiceMember[] | undefined;
type ClassResolver = (name: string) => ClassInfo | undefined;

function makeChoicesResolver(
    importer: { file: string; localChoices: Map<string, ChoiceMember[]>; imports: Map<string, ImportTarget> },
    byFile: Map<string, Map<string, ChoiceMember[]>>,
    global: Map<string, ChoiceMember[]>,
): ChoicesResolver {
    return (name: string) => {
        const local = importer.localChoices.get(name);
        if (local) return local;
        const imp = importer.imports.get(name);
        if (imp) {
            const targetFile = resolveImportTarget(importer.file, imp);
            if (targetFile) {
                const fileMap = byFile.get(targetFile);
                if (fileMap) {
                    const m = fileMap.get(imp.originalName);
                    if (m) return m;
                }
            }
        }
        return global.get(name);
    };
}

function makeClassResolver(
    importer: { file: string; localClasses: Map<string, ClassInfo>; imports: Map<string, ImportTarget> },
    byFile: Map<string, Map<string, ClassInfo>>,
    global: Map<string, ClassInfo>,
): ClassResolver {
    return (name: string) => {
        const local = importer.localClasses.get(name);
        if (local) return local;
        const imp = importer.imports.get(name);
        if (imp) {
            const targetFile = resolveImportTarget(importer.file, imp);
            if (targetFile) {
                const fileMap = byFile.get(targetFile);
                if (fileMap) {
                    const info = fileMap.get(imp.originalName);
                    if (info) return info;
                }
            }
        }
        return global.get(name);
    };
}

/**
 * Resolve an `ImportTarget` to an absolute file path (without the
 * `.py` suffix — we match against cached file paths). Returns
 * undefined when no cached file matches.
 *
 * Resolution rules:
 *   - Relative: walk up `relativeDepth` directories from the
 *     importer's dir, then descend into `modulePath`. Match against
 *     `<resolved>/models.py` (the only file shape in our cache).
 *   - Absolute: scan every cached file for one whose tail-path
 *     matches `<modulePath>/models.py`. This is a heuristic — proper
 *     absolute resolution needs the project's `INSTALLED_APPS` /
 *     `sys.path`, which we don't have at extractor time.
 */
function resolveImportTarget(
    importerFile: string,
    imp: ImportTarget,
): string | undefined {
    const importerDir = path.dirname(importerFile);
    if (imp.isRelative) {
        let dir = importerDir;
        for (let i = 1; i < imp.relativeDepth; i++) {
            dir = path.dirname(dir);
        }
        // Map `<rest>` → `<dir>/<rest>/models.py`. If the import
        // target is the importing file's own package's `models.py`
        // (i.e. `from . import x` → modulePath empty), no extractor
        // file exists for it; bail.
        if (imp.modulePath === "" || imp.modulePath === "models") {
            return path.join(dir, "models.py");
        }
        return path.join(dir, ...imp.modulePath.split("/"), "models.py");
    }
    // Absolute import — heuristic suffix match against cached files.
    // Caller passes the resolver only the cached file map so we don't
    // need a separate lookup here.
    return undefined;
}

/**
 * Walk a parsed module's top-level imports and return a
 * `local_name → ImportTarget` map. Recognises:
 *
 *   - `from .x import Foo` → imports[Foo] = {kind: relative, depth: 1, ...}
 *   - `from .x import Foo as Bar` → imports[Bar] = {originalName: Foo, ...}
 *   - `from apps.x import Foo` → imports[Foo] = {kind: absolute, ...}
 *
 * Deliberately ignored:
 *   - `import x` / `import x.y` — these don't bring class names into
 *     scope.
 *   - `from x import *` — wildcard imports leave us guessing; the
 *     global fallback covers this case implicitly.
 */
function collectImports(root: SyntaxNode): Map<string, ImportTarget> {
    const out = new Map<string, ImportTarget>();
    for (let i = 0; i < root.namedChildCount; i++) {
        const stmt = root.namedChild(i);
        if (!stmt || stmt.type !== "import_from_statement") continue;
        const modSpec = stmt.namedChild(0);
        if (!modSpec) continue;

        let isRelative = false;
        let relativeDepth = 0;
        let modulePath = "";

        if (modSpec.type === "relative_import") {
            isRelative = true;
            const prefix = modSpec.namedChildren.find((c) => c.type === "import_prefix");
            relativeDepth = prefix ? prefix.text.length : 1;
            const dotted = modSpec.namedChildren.find((c) => c.type === "dotted_name");
            if (dotted) {
                modulePath = dottedAsPath(dotted);
            }
        } else if (modSpec.type === "dotted_name") {
            isRelative = false;
            modulePath = dottedAsPath(modSpec);
        } else {
            continue;
        }

        // Remaining named children describe what's imported.
        for (let j = 1; j < stmt.namedChildCount; j++) {
            const c = stmt.namedChild(j);
            if (!c) continue;
            if (c.type === "wildcard_import") continue;
            if (c.type === "aliased_import") {
                const orig = c.namedChild(0);
                const alias = c.childForFieldName("alias");
                if (!orig) continue;
                const original = lastSegmentOfDotted(orig);
                const localName = alias ? alias.text : original;
                out.set(localName, {
                    modulePath,
                    isRelative,
                    relativeDepth,
                    originalName: original,
                });
            } else if (c.type === "dotted_name") {
                const original = lastSegmentOfDotted(c);
                out.set(original, {
                    modulePath,
                    isRelative,
                    relativeDepth,
                    originalName: original,
                });
            }
        }
    }
    return out;
}

function dottedAsPath(node: SyntaxNode): string {
    const segs: string[] = [];
    for (let i = 0; i < node.namedChildCount; i++) {
        const c = node.namedChild(i);
        if (c && c.type === "identifier") segs.push(c.text);
    }
    return segs.join("/");
}

function lastSegmentOfDotted(node: SyntaxNode): string {
    if (node.type === "identifier") return node.text;
    if (node.type === "dotted_name") {
        for (let i = node.namedChildCount - 1; i >= 0; i--) {
            const c = node.namedChild(i);
            if (c && c.type === "identifier") return c.text;
        }
    }
    return node.text;
}

/** Walk one cached tree's class definitions and emit fragments. */
function emitFromTree(
    file: string,
    root: SyntaxNode,
    choicesResolver: ChoicesResolver,
    classResolver: ClassResolver,
    result: DjangoModelsScanResult,
): void {
    walk(root, (node) => {
        if (node.type !== "class_definition") return;
        const name = node.childForFieldName("name")?.text;
        if (!name) return;
        const info = classResolver(name);
        if (!info) return;
        if (info.isAbstract) return true; // abstract base — no own entity
        if (!chainReachesDjangoModel(name, classResolver)) {
            // Choices classes are handled in phase A; descending into them
            // now would re-trigger model-field heuristics on enum members.
            if (isChoicesClass(node)) return true;
            return;
        }
        emitFromClass(file, node, choicesResolver, classResolver, result);
        return true;
    });
}

async function* findModelsFiles(dir: string): AsyncGenerator<string> {
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
            // Common dirs that don't contain Django source.
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
            yield* findModelsFiles(full);
        } else if (entry === "models.py") {
            yield full;
        }
    }
}


/**
 * True iff `class_definition` node has at least one base in the
 * `models.Model` shape — `models.Model`, `Model`, or any subclass
 * referencing Django's Model. We accept dotted attribute access
 * (`models.Model`) and bare identifiers (`Model` after `from
 * django.db.models import Model`); both are the canonical idioms.
 *
 * Subclassed abstract bases (`class TimestampedModel(models.Model)` →
 * `class Foo(TimestampedModel)`) are not detected here — the caller
 * sees an isolated class. v0.5 deliberately leaves this gap; full
 * inheritance resolution lands when the merge engine grows
 * cross-file vocabulary.
 */
/** One member of a `TextChoices` / `IntegerChoices` class. */
interface ChoiceMember {
    rawValue: string;
    label: string;
    /** 1-indexed line of the assignment. */
    line: number;
}

/**
 * True iff the class subclasses `models.TextChoices`,
 * `models.IntegerChoices`, or the bare imports thereof. We do NOT
 * accept arbitrary `Choices`-suffixed classes — Django ships these
 * three, and matching by name suffix would false-positive on
 * unrelated enum-shaped classes (`PriorityChoices(Enum)`).
 */
function isChoicesClass(classNode: SyntaxNode): boolean {
    const supers = classNode.childForFieldName("superclasses");
    if (!supers) return false;
    for (let i = 0; i < supers.namedChildCount; i++) {
        const base = supers.namedChild(i);
        if (!base) continue;
        const inner = base.type === "argument" && base.namedChildCount > 0
            ? base.namedChild(0)!
            : base;
        const text = inner.text.trim();
        if (
            text === "models.TextChoices" ||
            text === "models.IntegerChoices" ||
            text === "models.Choices" ||
            text === "TextChoices" ||
            text === "IntegerChoices" ||
            text === "Choices"
        ) {
            return true;
        }
    }
    return false;
}

/**
 * Walk the tree once and collect every TextChoices / IntegerChoices
 * class definition into a `class_name → members[]` map. Members are
 * extracted from `NAME = raw, label` (tuple shorthand without parens,
 * which tree-sitter exposes as `expression_list`) and `NAME = (raw,
 * label)` (parens, exposed as `tuple`).
 */
function collectChoicesClasses(root: SyntaxNode): Map<string, ChoiceMember[]> {
    const out = new Map<string, ChoiceMember[]>();
    walk(root, (node) => {
        if (node.type !== "class_definition") return;
        if (!isChoicesClass(node)) return;
        const name = node.childForFieldName("name")?.text;
        const body = node.childForFieldName("body");
        if (!name || !body) return true;
        const members: ChoiceMember[] = [];
        for (let i = 0; i < body.namedChildCount; i++) {
            const stmt = body.namedChild(i);
            if (!stmt || stmt.type !== "expression_statement") continue;
            const a = stmt.namedChild(0);
            if (!a || a.type !== "assignment") continue;
            const lhs = a.childForFieldName("left");
            const rhs = a.childForFieldName("right");
            if (!lhs || !rhs) continue;
            if (lhs.type !== "identifier") continue;
            const pair = readChoicesMemberRhs(rhs);
            if (!pair) continue;
            members.push({
                rawValue: pair.raw,
                label: pair.label,
                line: (a.startPosition.row ?? 0) + 1,
            });
        }
        out.set(name, members);
        return true;
    });
    return out;
}

/**
 * Decode the rhs of `NAME = raw, label` / `NAME = (raw, label)`. Single
 * raw values without a label (Django auto-generates the label from the
 * member name) are intentionally skipped — the SemanticGraph would
 * otherwise pick up titlecased member names that don't match real
 * vocabulary.
 */
function readChoicesMemberRhs(rhs: SyntaxNode): { raw: string; label: string } | null {
    const node =
        rhs.type === "tuple" || rhs.type === "expression_list" ? rhs : null;
    if (!node || node.namedChildCount < 2) return null;
    const rawNode = node.namedChild(0)!;
    const labelNode = node.namedChild(1)!;
    const raw = parseChoiceRaw(rawNode);
    if (raw === null) return null;
    if (labelNode.type !== "string") return null;
    const label = parsePythonStringLiteral(labelNode);
    if (label === null) return null;
    return { raw, label };
}

/**
 * One class_definition's metadata, used by the inheritance resolver.
 * `superclassNames` are the local-name segments — for `models.Model`,
 * `Timestamped`, `app.shared.Mixin` we keep `Model`, `Timestamped`,
 * `Mixin`. The local-name match keeps cross-file cases at least
 * detectable; full path resolution is a v1.0 enhancement.
 */
interface ClassInfo {
    name: string;
    body: SyntaxNode;
    superclassNames: string[];
    /** True when `class Meta: abstract = True` is set inside the class. */
    isAbstract: boolean;
}

function collectClassIndex(root: SyntaxNode): Map<string, ClassInfo> {
    const out = new Map<string, ClassInfo>();
    walk(root, (node) => {
        if (node.type !== "class_definition") return;
        const name = node.childForFieldName("name")?.text;
        const body = node.childForFieldName("body");
        if (!name || !body) return;
        const supers = node.childForFieldName("superclasses");
        const superclassNames: string[] = [];
        if (supers) {
            for (let i = 0; i < supers.namedChildCount; i++) {
                const base = supers.namedChild(i);
                if (!base) continue;
                const inner = base.type === "argument" && base.namedChildCount > 0
                    ? base.namedChild(0)!
                    : base;
                const tail = lastTextSegment(inner);
                if (tail !== null) superclassNames.push(tail);
            }
        }
        out.set(name, {
            name,
            body,
            superclassNames,
            isAbstract: readAbstractFlag(body),
        });
        return; // keep descending — nested class_definitions exist (Meta).
    });
    return out;
}

function lastTextSegment(node: SyntaxNode): string | null {
    if (node.type === "identifier") return node.text;
    if (node.type === "attribute") {
        const tail = node.childForFieldName("attribute");
        if (tail) return tail.text;
        return node.text.split(".").slice(-1)[0] ?? null;
    }
    return null;
}

/** True iff `class Meta: abstract = True` (or `... = 1`) inside `body`. */
function readAbstractFlag(body: SyntaxNode): boolean {
    for (let i = 0; i < body.namedChildCount; i++) {
        const stmt = body.namedChild(i);
        if (!stmt || stmt.type !== "class_definition") continue;
        if (stmt.childForFieldName("name")?.text !== "Meta") continue;
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
            if (lhs.type !== "identifier" || lhs.text !== "abstract") continue;
            if (rhs.type === "true") return true;
            if (rhs.type === "integer" && rhs.text !== "0") return true;
        }
    }
    return false;
}

/**
 * True iff `name`'s superclass chain reaches a direct `models.Model`
 * (or bare `Model`) base. The classResolver bridges file-local
 * lookups, import-resolved lookups, and the global bare-name
 * fallback — so cross-file inheritance through imported abstract
 * bases resolves correctly. Cycles are guarded by a depth budget;
 * pathological inheritance is treated as "doesn't reach Model" so
 * the walker stays safe.
 */
function chainReachesDjangoModel(
    name: string,
    classResolver: ClassResolver,
): boolean {
    const seen = new Set<string>();
    const stack = [name];
    let budget = 64;
    while (stack.length > 0 && budget-- > 0) {
        const cur = stack.pop()!;
        if (seen.has(cur)) continue;
        seen.add(cur);
        const info = classResolver(cur);
        if (!info) continue;
        for (const s of info.superclassNames) {
            if (s === "Model") return true; // direct django Model base
            if (classResolver(s)) stack.push(s);
        }
    }
    return false;
}

/**
 * Walk the inheritance chain depth-first and return every ancestor
 * `ClassInfo` whose body should contribute fields to `name`.
 * The class itself is NOT included — callers walk the class body
 * directly. Order: most-distant ancestor first, closest ancestor
 * last, so a closer override naturally supersedes a further one
 * when fragments collide.
 */
function ancestorsContributingFields(
    name: string,
    classResolver: ClassResolver,
): ClassInfo[] {
    const result: ClassInfo[] = [];
    const seen = new Set<string>([name]);
    const visit = (cur: string): void => {
        const info = classResolver(cur);
        if (!info) return;
        for (const s of info.superclassNames) {
            if (s === "Model" || s === "models.Model") continue;
            if (seen.has(s)) continue;
            seen.add(s);
            visit(s);
            const superInfo = classResolver(s);
            if (superInfo) result.push(superInfo);
        }
    };
    visit(name);
    return result;
}

interface MetaInfo {
    dbTable: string | null;
    verboseName: string | null;
    verboseNamePlural: string | null;
}

function emitFromClass(
    file: string,
    classNode: SyntaxNode,
    choicesResolver: ChoicesResolver,
    classResolver: ClassResolver,
    result: DjangoModelsScanResult,
): void {
    const className = classNode.childForFieldName("name")?.text;
    if (!className) return;

    const body = classNode.childForFieldName("body");
    if (!body) return;

    const meta = readMetaInner(body);
    const canonicalEntityRaw = meta.dbTable ?? toSnakeCase(className);

    let canonicalEntity: string;
    try {
        canonicalEntity = sanitiseCanonical(canonicalEntityRaw);
    } catch (e) {
        if (e instanceof SanitiserError) {
            result.skipped.push({ file, reason: e.message });
            return;
        }
        throw e;
    }

    const classLine = (classNode.startPosition.row ?? 0) + 1;

    if (meta.verboseName) {
        emitEntityLabel(file, canonicalEntity, meta.verboseName, classLine, result);
    }
    if (meta.verboseNamePlural) {
        emitEntityLabel(file, canonicalEntity, meta.verboseNamePlural, classLine, result);
    } else if (meta.verboseName === null) {
        // No explicit verbose_name → emit the class name humanised so
        // the SemanticGraph still has a fallback singular label.
        emitEntityLabel(
            file,
            canonicalEntity,
            humaniseClassName(className),
            classLine,
            result,
        );
    }

    // Field walk — own fields plus those declared on every ancestor
    // (in-file or import-resolved). Ancestors first so their
    // fragments are in the same logical order they'd appear in a
    // fully-flattened class body.
    const ancestors = ancestorsContributingFields(className, classResolver);
    for (const a of ancestors) {
        emitFieldsFromBody(file, canonicalEntity, a.body, choicesResolver, result);
    }
    emitFieldsFromBody(file, canonicalEntity, body, choicesResolver, result);
}

function emitFieldsFromBody(
    file: string,
    canonicalEntity: string,
    body: SyntaxNode,
    choicesResolver: ChoicesResolver,
    result: DjangoModelsScanResult,
): void {
    for (let i = 0; i < body.namedChildCount; i++) {
        const stmt = body.namedChild(i);
        if (!stmt) continue;
        if (stmt.type !== "expression_statement") continue;
        const inner = stmt.namedChild(0);
        if (!inner) continue;
        if (inner.type !== "assignment") continue;
        emitFromAssignment(file, canonicalEntity, inner, choicesResolver, result);
    }
}

function emitEntityLabel(
    file: string,
    canonicalEntity: string,
    rawLabel: string,
    line: number,
    result: DjangoModelsScanResult,
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
        canonical: { kind: "entity", entity: canonicalEntity },
        confidence: 0.9,
        locator: {
            file,
            line,
            layer: SourceLayer.Orm,
            extractor: "extractor-django:models",
        },
    });
}

function emitFromAssignment(
    file: string,
    canonicalEntity: string,
    assign: SyntaxNode,
    choicesResolver: ChoicesResolver,
    result: DjangoModelsScanResult,
): void {
    const lhs = assign.childForFieldName("left");
    const rhs = assign.childForFieldName("right");
    if (!lhs || !rhs) return;
    if (lhs.type !== "identifier") return;
    if (rhs.type !== "call") return;
    const callee = rhs.childForFieldName("function");
    if (!callee) return;
    // Accept `models.X(...)` and `X(...)` (X imported from
    // django.db.models). The canonical idiom is `models.X`; we keep
    // the bare form as a defensive fallback for projects that import
    // every field type explicitly.
    const calleeText = callee.text.trim();
    const isModelsCall = calleeText.startsWith("models.") || /^[A-Z][A-Za-z0-9]*Field$/.test(calleeText);
    if (!isModelsCall) return;

    const fieldName = lhs.text;
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
    const args = rhs.childForFieldName("arguments");
    if (!args) return;

    const verbose = readFieldLabel(args);
    if (verbose !== null) {
        emitFieldLabel(
            file,
            canonicalEntity,
            canonicalField,
            verbose,
            line,
            SourceLayer.Orm,
            result,
        );
    }

    const help = readKwarg(args, "help_text");
    if (help !== null) {
        emitFieldLabel(
            file,
            canonicalEntity,
            canonicalField,
            help,
            line,
            SourceLayer.ApiResource,
            result,
        );
    }

    emitChoicesIfPresent(file, canonicalEntity, canonicalField, args, choicesResolver, result);
}

/**
 * Walk a `choices=[(raw, label), ...]` (or tuple-of-tuples) kwarg
 * and emit one `enum_value` fragment per pair. We accept:
 *
 *   - List or tuple of inner pairs.
 *   - Inner pairs that are themselves tuples or lists of length 2.
 *   - Raw element: string, integer, float, true/false, None.
 *   - Label element: static string literal only.
 *
 * Anything else (a bare reference like `choices=Status.choices`, an
 * f-string label, an `_("…")` call) is silently skipped — Django's
 * `TextChoices` / `IntegerChoices` enum-class idioms land in a v0.6
 * follow-up that walks the choices class itself.
 */
function emitChoicesIfPresent(
    file: string,
    canonicalEntity: string,
    canonicalField: string,
    args: SyntaxNode,
    choicesResolver: ChoicesResolver,
    result: DjangoModelsScanResult,
): void {
    const choicesNode = findKwargValue(args, "choices");
    if (!choicesNode) return;
    const enumName = `${canonicalEntity}.${canonicalField}`;

    // Resolve `choices=Status.choices` (or any
    // `<ClassName>.choices` reference) via the import-aware resolver.
    // Lookup priority: file-local class registry → imports → global
    // bare-name fallback. The attribute access pattern is the
    // canonical idiom for TextChoices / IntegerChoices.
    if (choicesNode.type === "attribute") {
        const ownerName = choicesNode.childForFieldName("object");
        const attrName = choicesNode.childForFieldName("attribute");
        if (!ownerName || !attrName) return;
        if (attrName.text !== "choices") return;
        if (ownerName.type !== "identifier") return;
        const members = choicesResolver(ownerName.text);
        if (!members) return;
        for (const m of members) {
            emitEnumMember(file, enumName, m.rawValue, m.label, m.line, result);
        }
        return;
    }

    if (choicesNode.type !== "list" && choicesNode.type !== "tuple") return;

    for (let i = 0; i < choicesNode.namedChildCount; i++) {
        const pair = choicesNode.namedChild(i);
        if (!pair) continue;
        if (pair.type !== "tuple" && pair.type !== "list") continue;
        if (pair.namedChildCount < 2) continue;
        const rawNode = pair.namedChild(0)!;
        const labelNode = pair.namedChild(1)!;
        const raw = parseChoiceRaw(rawNode);
        if (raw === null) continue;
        if (labelNode.type !== "string") continue;
        const label = parsePythonStringLiteral(labelNode);
        if (label === null) continue;
        emitEnumMember(
            file,
            enumName,
            raw,
            label,
            (pair.startPosition.row ?? 0) + 1,
            result,
        );
    }
}

function emitEnumMember(
    file: string,
    enumName: string,
    rawValue: string,
    rawLabel: string,
    line: number,
    result: DjangoModelsScanResult,
): void {
    let cleanLabel: string;
    try {
        cleanLabel = sanitiseLabel(rawLabel);
    } catch (e) {
        if (e instanceof SanitiserError) {
            result.skipped.push({ file, reason: e.message });
            return;
        }
        throw e;
    }
    result.fragments.push({
        term: cleanLabel.toLowerCase(),
        canonical: { kind: "enum_value", enumName, rawValue },
        confidence: 0.85,
        locator: {
            file,
            line,
            layer: SourceLayer.Orm,
            extractor: "extractor-django:models",
        },
    });
}

/**
 * Parse a Python literal that's allowed as a choices raw value.
 * Returns the canonical string form (Django stores raw values as
 * strings/ints in the DB; the SemanticGraph stores them stringified
 * for uniform comparison against runtime query values).
 */
function parseChoiceRaw(node: SyntaxNode): string | null {
    switch (node.type) {
        case "string":
            return parsePythonStringLiteral(node);
        case "integer":
            // tree-sitter-python tolerates `1_000` / `0x10` etc. —
            // drop digit separators so the canonical form round-trips
            // through SQL parameter binding cleanly. Hex/oct/bin
            // prefixes are kept verbatim; the rewriter handles them.
            return node.text.replace(/_/g, "");
        case "float":
            return node.text.replace(/_/g, "");
        case "true":
            return "True";
        case "false":
            return "False";
        case "none":
            return "None";
        default:
            return null;
    }
}

function emitFieldLabel(
    file: string,
    canonicalEntity: string,
    canonicalField: string,
    rawLabel: string,
    line: number,
    layer: SourceLayer,
    result: DjangoModelsScanResult,
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
            layer,
            extractor: "extractor-django:models",
        },
    });
}

/**
 * Pull the field's display label out of a `models.X(...)` call's
 * argument list. Two idioms:
 *
 *   1. `models.CharField(verbose_name="Display")` — keyword argument.
 *   2. `models.CharField("Display", max_length=255)` — first positional
 *      argument is the verbose_name (Django's convention).
 *
 * Returns `null` when neither form is found or when the would-be
 * label is non-static (an i18n call like `_("Display")`, an f-string,
 * etc.). We deliberately don't try to unwrap `_()` here — that's
 * the i18n extractor's job.
 */
function readFieldLabel(args: SyntaxNode): string | null {
    const kw = readKwarg(args, "verbose_name");
    if (kw !== null) return kw;

    // First positional → check arg 0 if it's a string literal.
    for (let i = 0; i < args.namedChildCount; i++) {
        const c = args.namedChild(i);
        if (!c) continue;
        if (c.type === "keyword_argument") return null; // hit kwargs first → no positional verbose_name
        if (c.type === "string") {
            return parsePythonStringLiteral(c);
        }
        // Any other positional kind (call expression, identifier, etc.)
        // is not a static label — bail.
        return null;
    }
    return null;
}

/**
 * Locate `kw_name=<value>` in an argument list and return the value
 * node. Returns `null` if the kwarg is absent. Used by the choices
 * walker (where we want the raw expression node, not a parsed string).
 */
function findKwargValue(args: SyntaxNode, name: string): SyntaxNode | null {
    for (let i = 0; i < args.namedChildCount; i++) {
        const c = args.namedChild(i);
        if (!c || c.type !== "keyword_argument") continue;
        const kwName = c.childForFieldName("name");
        if (!kwName || kwName.text !== name) continue;
        return c.childForFieldName("value");
    }
    return null;
}

function readKwarg(args: SyntaxNode, name: string): string | null {
    for (let i = 0; i < args.namedChildCount; i++) {
        const c = args.namedChild(i);
        if (!c) continue;
        if (c.type !== "keyword_argument") continue;
        const kwName = c.childForFieldName("name");
        const kwVal = c.childForFieldName("value");
        if (!kwName || !kwVal) continue;
        if (kwName.text !== name) continue;
        if (kwVal.type === "string") {
            return parsePythonStringLiteral(kwVal);
        }
        return null;
    }
    return null;
}

function readMetaInner(classBody: SyntaxNode): MetaInfo {
    const meta: MetaInfo = { dbTable: null, verboseName: null, verboseNamePlural: null };
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
            if (lhs.type !== "identifier") continue;
            if (rhs.type !== "string") continue;
            const value = parsePythonStringLiteral(rhs);
            if (value === null) continue;
            if (lhs.text === "db_table") meta.dbTable = value;
            else if (lhs.text === "verbose_name") meta.verboseName = value;
            else if (lhs.text === "verbose_name_plural") meta.verboseNamePlural = value;
        }
    }
    return meta;
}

/**
 * Convert `CamelCase` → `camel_case`. Mirrors Django's default
 * `db_table` derivation (lower(class_name)) — Django actually drops
 * the underscores AND lowercases, but we preserve underscores so
 * collisions across CamelCase and Camel_Case classes surface in the
 * conflict log instead of silently merging.
 */
export function toSnakeCase(name: string): string {
    return name
        .replace(/([a-z0-9])([A-Z])/g, "$1_$2")
        .replace(/([A-Z]+)([A-Z][a-z])/g, "$1_$2")
        .toLowerCase();
}

/**
 * Convert `CamelCaseName` → `Camel Case Name` for the singular-label
 * fallback. Used when no `Meta.verbose_name` is supplied.
 */
function humaniseClassName(name: string): string {
    return name
        .replace(/([a-z0-9])([A-Z])/g, "$1 $2")
        .replace(/([A-Z]+)([A-Z][a-z])/g, "$1 $2");
}
