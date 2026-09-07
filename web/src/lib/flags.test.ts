import { describe, expect, it } from "vitest";

import { COUNTRIES } from "./countries";
import { countryFlag, driverNameWithFlag } from "./flags";

describe("countryFlag", () => {
  it("has a flag for every country the dropdown offers", () => {
    // A country added to COUNTRIES without a matching code here would
    // silently show no flag rather than fail loudly -- this is the guard
    // against that gap across a ~195-entry manual mapping.
    for (const country of COUNTRIES) {
      expect(countryFlag(country), `no flag for ${country}`).not.toBeNull();
    }
  });

  it("renders a two-codepoint regional-indicator flag for a known country", () => {
    expect(countryFlag("New Zealand")).toBe("🇳🇿");
    expect(countryFlag("United States")).toBe("🇺🇸");
    expect(countryFlag("United Kingdom")).toBe("🇬🇧");
  });

  it("returns null for unknown or missing input", () => {
    expect(countryFlag(null)).toBeNull();
    expect(countryFlag(undefined)).toBeNull();
    expect(countryFlag("")).toBeNull();
    expect(countryFlag("Not A Country")).toBeNull();
  });

  it("gives every country a distinct flag", () => {
    const flags = COUNTRIES.map((c) => countryFlag(c));
    expect(new Set(flags).size).toBe(COUNTRIES.length);
  });
});

describe("driverNameWithFlag", () => {
  it("precedes the name with the flag when the country is known", () => {
    expect(driverNameWithFlag("Alice", "New Zealand")).toBe("🇳🇿 Alice");
  });

  it("returns the plain name when the country is unknown or unset", () => {
    expect(driverNameWithFlag("Alice", null)).toBe("Alice");
    expect(driverNameWithFlag("Alice", undefined)).toBe("Alice");
  });
});
