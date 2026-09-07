import { describe, expect, it } from "vitest";

import { COUNTRIES } from "./countries";
import { countryCode } from "./flags";

describe("countryCode", () => {
  it("has a code for every country the dropdown offers", () => {
    // A country added to COUNTRIES without a matching code here would
    // silently show no flag rather than fail loudly -- this is the guard
    // against that gap across a ~196-entry manual mapping.
    for (const country of COUNTRIES) {
      expect(countryCode(country), `no code for ${country}`).not.toBeNull();
    }
  });

  it("lowercases the code for use as a flag-icons CSS class", () => {
    expect(countryCode("New Zealand")).toBe("nz");
    expect(countryCode("United States")).toBe("us");
    expect(countryCode("United Kingdom")).toBe("gb");
  });

  it("maps England to flag-icons' home-nation extension code, not a real ISO entry", () => {
    expect(countryCode("England")).toBe("gb-eng");
  });

  it("returns null for unknown or missing input", () => {
    expect(countryCode(null)).toBeNull();
    expect(countryCode(undefined)).toBeNull();
    expect(countryCode("")).toBeNull();
    expect(countryCode("Not A Country")).toBeNull();
  });

  it("gives every country a distinct code", () => {
    const codes = COUNTRIES.map((c) => countryCode(c));
    expect(new Set(codes).size).toBe(COUNTRIES.length);
  });
});
