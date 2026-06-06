import { describe, expect, it } from "vitest";
import {
	SanitiserError,
	sanitiseCanonical,
	sanitiseLabel,
} from "./sanitise.js";

describe("sanitiseCanonical", () => {
	it.each(["users", "tenant_id", "_private", "col42", "a"])(
		"accepts %s",
		(n) => {
			expect(sanitiseCanonical(n)).toBe(n);
		},
	);

	it.each([
		"",
		"1users",
		"users-table",
		"active OR 1=1",
		"users; DROP",
		"users.col",
		"users ",
	])("rejects %s", (n) => {
		expect(() => sanitiseCanonical(n)).toThrow(SanitiserError);
	});

	it("rejects non-string input", () => {
		expect(() => sanitiseCanonical(42 as unknown as string)).toThrow(
			SanitiserError,
		);
	});
});

describe("sanitiseLabel", () => {
	it("accepts normal labels", () => {
		expect(sanitiseLabel("Joined Date")).toBe("Joined Date");
	});

	it("strips zero-width characters", () => {
		expect(sanitiseLabel("Stu​dents")).toBe("Students");
	});

	it("normalises NFC", () => {
		expect(sanitiseLabel("café")).toBe(sanitiseLabel("café"));
	});

	it("rejects empty after sanitisation", () => {
		expect(() => sanitiseLabel("   ")).toThrow(SanitiserError);
	});

	it("caps length", () => {
		expect(() => sanitiseLabel("a".repeat(1024))).toThrow(SanitiserError);
	});
});
