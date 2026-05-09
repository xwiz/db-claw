import { describe, expect, it } from "vitest";

import {
    extractMakeI18nChainsAst,
    extractMakeLabelChainsAst,
} from "./php-ast.js";

const guardOrSkip = <T>(value: T | null): T => {
    if (value === null) {
        // tree-sitter-php native binding unavailable on this CPU — the
        // regex path takes over in production. The unit tests here only
        // run in environments where the AST walker is available.
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        (globalThis as any).__skip = true;
        throw new Error("tree-sitter-php unavailable; skipping AST tests");
    }
    return value;
};

describe("extractMakeLabelChainsAst — baseline (Filament v3+v4 share this shape)", () => {
    it("captures the canonical Type::make()->label() chain", () => {
        const src = `<?php
        TextInput::make('full_name')->label('Full Name');`;
        const chains = guardOrSkip(extractMakeLabelChainsAst(src));
        expect(chains).toEqual([
            { field: "full_name", label: "Full Name", line: 2 },
        ]);
    });

    it("captures Filament v4 Schemas\\Components\\Field shapes", () => {
        // v4 introduced the `Filament\Schemas\Components\Field` builder;
        // syntactically identical to v3 forms — Type::make()->label().
        const src = `<?php
        Filament\\Schemas\\Components\\Field::make('balance')
            ->numeric()
            ->required()
            ->label('Account Balance');`;
        const chains = guardOrSkip(extractMakeLabelChainsAst(src));
        expect(chains).toHaveLength(1);
        expect(chains[0].field).toBe("balance");
        expect(chains[0].label).toBe("Account Balance");
    });

    it("rejects label() with a __() i18n call (handled by the i18n walker)", () => {
        const src = `<?php
        TextInput::make('full_name')->label(__('users.full_name'));`;
        const chains = guardOrSkip(extractMakeLabelChainsAst(src));
        expect(chains).toEqual([]);
    });
});

describe("extractMakeI18nChainsAst — i18n-bound labels", () => {
    it("captures Type::make()->label(__('lang.key'))", () => {
        const src = `<?php
        TextInput::make('full_name')->label(__('users.full_name'));`;
        const chains = guardOrSkip(extractMakeI18nChainsAst(src));
        expect(chains).toEqual([
            { field: "full_name", i18nKey: "users.full_name", line: 2 },
        ]);
    });

    it("captures chains with intermediate modifiers", () => {
        const src = `<?php
        TextInput::make('balance')
            ->numeric()
            ->required()
            ->label(__('billing.balance'));`;
        const chains = guardOrSkip(extractMakeI18nChainsAst(src));
        expect(chains).toHaveLength(1);
        expect(chains[0].i18nKey).toBe("billing.balance");
    });

    it("works on the v4 Schema-component namespace path", () => {
        const src = `<?php
        Filament\\Schemas\\Components\\Field::make('email')
            ->label(__('users.email_label'));`;
        const chains = guardOrSkip(extractMakeI18nChainsAst(src));
        expect(chains).toEqual([
            { field: "email", i18nKey: "users.email_label", line: 2 },
        ]);
    });

    it("rejects __() with replacements (string is dynamic)", () => {
        const src = `<?php
        TextInput::make('count')->label(__('items.count', ['count' => 5]));`;
        // Multi-arg __() can interpolate, so treating it as a static
        // i18n key would be unsafe — drop.
        const chains = guardOrSkip(extractMakeI18nChainsAst(src));
        expect(chains).toEqual([]);
    });

    it("rejects __() with a non-static-string key", () => {
        const src = `<?php
        TextInput::make('x')->label(__($lang_key));`;
        const chains = guardOrSkip(extractMakeI18nChainsAst(src));
        expect(chains).toEqual([]);
    });

    it("rejects literal-string label() (those go to the literal walker)", () => {
        const src = `<?php
        TextInput::make('x')->label('Plain Label');`;
        const chains = guardOrSkip(extractMakeI18nChainsAst(src));
        expect(chains).toEqual([]);
    });

    it("returns empty on input with no make/label chain", () => {
        const src = `<?php
        $x = 1;
        function foo() { return 2; }`;
        const chains = guardOrSkip(extractMakeI18nChainsAst(src));
        expect(chains).toEqual([]);
    });

    it("captures multiple chains in one file", () => {
        const src = `<?php
        TextInput::make('first')->label(__('a.first'));
        TextInput::make('second')->label(__('a.second'));`;
        const chains = guardOrSkip(extractMakeI18nChainsAst(src));
        expect(chains.map((c) => c.field)).toEqual(["first", "second"]);
        expect(chains.map((c) => c.i18nKey)).toEqual(["a.first", "a.second"]);
    });
});
