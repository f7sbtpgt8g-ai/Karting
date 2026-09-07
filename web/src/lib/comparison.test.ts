import { describe, expect, it } from "vitest";

import { lapKey, lapLabel, lapsBySession, parseLapKey, tabLabels } from "./comparison";

describe("lap keys", () => {
  it("keeps two drivers' identical lap numbers apart", () => {
    // The whole reason keys exist: without the session, both of these are
    // "10", the selection collapses to one entry, and one driver's lap
    // silently stops being plotted.
    expect(lapKey(11, 10)).not.toBe(lapKey(12, 10));
  });

  it("round-trips", () => {
    expect(parseLapKey(lapKey(482, 7))).toEqual({ sessionId: 482, lapNumber: 7 });
  });
});

describe("lapsBySession", () => {
  it("groups a mixed selection by session, in lap order", () => {
    const grouped = lapsBySession([lapKey(2, 9), lapKey(1, 4), lapKey(2, 3), lapKey(1, 12)]);
    expect(grouped.get(1)).toEqual([4, 12]);
    expect(grouped.get(2)).toEqual([3, 9]);
  });

  it("never carries a lap number to a session it did not come from", () => {
    const grouped = lapsBySession([lapKey(1, 10), lapKey(2, 20)]);
    expect(grouped.get(1)).toEqual([10]);
    expect(grouped.get(2)).toEqual([20]);
    expect(grouped.get(1)).not.toContain(20);
  });

  it("de-duplicates and ignores malformed keys", () => {
    const grouped = lapsBySession([lapKey(1, 5), lapKey(1, 5), "nonsense", ":", "x:1"]);
    expect(grouped.get(1)).toEqual([5]);
    expect(grouped.size).toBe(1);
  });

  it("is empty for an empty selection", () => {
    expect(lapsBySession([]).size).toBe(0);
  });
});

describe("lapLabel", () => {
  it("leaves the driver off while only one session is open", () => {
    expect(lapLabel(10, "Oliver", { multiSession: false })).toBe("Lap 10");
  });

  it("leads with the driver once there is more than one", () => {
    expect(lapLabel(10, "Oliver", { multiSession: true })).toBe("Oliver · Lap 10");
  });

  it("adds the qualifier when one driver has two sessions open", () => {
    expect(lapLabel(3, "Oliver", { multiSession: true, qualifier: "14:05" })).toBe(
      "Oliver 14:05 · Lap 3",
    );
  });
});

describe("tabLabels", () => {
  const session = (id: number, driverName: string, startDate: string, startTime: string) => ({
    sessionId: id,
    driverName,
    startDate,
    startTime,
  });

  it("uses bare names when every driver appears once", () => {
    const labels = tabLabels([
      session(1, "Oliver", "29-08-2026", "10:05"),
      session(2, "Mika", "29-08-2026", "10:05"),
    ]);
    expect(labels.get(1)).toEqual({ name: "Oliver", qualifier: null });
    expect(labels.get(2)).toEqual({ name: "Mika", qualifier: null });
  });

  it("separates one driver's two days by date", () => {
    const labels = tabLabels([
      session(1, "Oliver", "29-08-2026", "10:05"),
      session(2, "Oliver", "30-08-2026", "10:05"),
    ]);
    expect(labels.get(1)?.qualifier).toBe("29-08-2026");
    expect(labels.get(2)?.qualifier).toBe("30-08-2026");
  });

  it("separates one driver's two sessions on the same day by time", () => {
    const labels = tabLabels([
      session(1, "Oliver", "29-08-2026", "10:05:00"),
      session(2, "Oliver", "29-08-2026", "14:33:00"),
    ]);
    expect(labels.get(1)?.qualifier).toBe("10:05");
    expect(labels.get(2)?.qualifier).toBe("14:33");
  });

  it("does not qualify a driver who only appears once alongside a repeated one", () => {
    const labels = tabLabels([
      session(1, "Oliver", "29-08-2026", "10:05"),
      session(2, "Oliver", "30-08-2026", "10:05"),
      session(3, "Mika", "29-08-2026", "10:05"),
    ]);
    expect(labels.get(3)).toEqual({ name: "Mika", qualifier: null });
  });
});
