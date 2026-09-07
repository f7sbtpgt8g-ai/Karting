import { describe, expect, it } from "vitest";

import { bulkOutcome } from "./writes";

describe("bulkOutcome", () => {
  it("reports everything saved when the database confirms every row", () => {
    const outcome = bulkOutcome([1, 2, 3], [1, 2, 3]);
    expect([...outcome.saved].sort()).toEqual([1, 2, 3]);
    expect(outcome.refused).toEqual([]);
    expect(outcome.message).toBeNull();
  });

  it("treats a row the database did not return as not saved", () => {
    // The bug this exists for: RLS filtering one row out of a bulk update is
    // not an error, so a table updated from the request would show all three
    // renamed and lose the third on the next page load.
    const outcome = bulkOutcome([1, 2, 3], [1, 2]);
    expect(outcome.saved.has(3)).toBe(false);
    expect(outcome.refused).toEqual([3]);
    expect(outcome.message).toMatch(/1 of 3/);
  });

  it("says so plainly when nothing was written at all", () => {
    const outcome = bulkOutcome([7, 8], []);
    expect(outcome.saved.size).toBe(0);
    expect(outcome.refused).toEqual([7, 8]);
    expect(outcome.message).toMatch(/None of the 2/);
  });

  it("keeps refused rows selectable in the order they were sent", () => {
    const outcome = bulkOutcome([10, 20, 30, 40], [20]);
    expect(outcome.refused).toEqual([10, 30, 40]);
  });

  it("ignores ids the database returns that were never asked for", () => {
    // Not expected from PostgREST, but "saved" must never grow beyond what
    // was requested -- it is used to decide which rows to redraw.
    const outcome = bulkOutcome([1, 2], [1, 2, 99]);
    expect([...outcome.saved].sort()).toEqual([1, 2]);
    expect(outcome.refused).toEqual([]);
  });

  it("reads correctly for a single session", () => {
    expect(bulkOutcome([5], []).message).toMatch(/1 selected session was/);
  });
});
