"use client";

import { useMemo } from "react";
import Link from "next/link";
import { sessionDate, sessionTime } from "@/lib/format";
import { POWERZONE_RPM, hasPowerzone } from "@/lib/engine";

export type EngineLapRow = {
  lapNumber: number;
  lapTimeS: number | null;
  excluded: boolean;
  sampleCount: number | null;
  maxSpeedKmh: number | null;
  minSpeedKmh: number | null;
  maxRpm: number | null;
  minRpm: number | null;
  avgRpm: number | null;
  maxTempC: number | null;
  minTempC: number | null;
  avgTempC: number | null;
  powerzonePct: number | null;
};

function lapClock(seconds: number | null): string {
  if (seconds === null || Number.isNaN(seconds)) return "—";
  const minutes = Math.floor(seconds / 60);
  return `${minutes}:${(seconds - minutes * 60).toFixed(3).padStart(6, "0")}`;
}

const num = (v: number | null, digits = 0) =>
  v === null || Number.isNaN(v) ? "—" : v.toFixed(digits);

export default function EngineAnalysis({
  sessionId,
  driverName,
  trackName,
  startDate,
  startTime,
  engineCategory,
  laps,
}: {
  sessionId: number;
  driverName: string;
  trackName: string | null;
  startDate: string | null;
  startTime: string | null;
  engineCategory: string | null;
  laps: EngineLapRow[];
}) {
  const showPowerzone = hasPowerzone(engineCategory);
  const included = useMemo(() => laps.filter((l) => !l.excluded), [laps]);

  const stats = useMemo(() => {
    const pick = (key: keyof EngineLapRow) =>
      included.map((l) => l[key] as number | null).filter((v): v is number => v !== null);

    /**
     * A session average over samples, not over laps.
     *
     * Averaging the per-lap averages would weight a three-corner out-lap the
     * same as a full one. Weighting by each lap's sample count gives the
     * mean the whole session actually ran at.
     */
    const weightedMean = (key: keyof EngineLapRow) => {
      let total = 0;
      let weight = 0;
      for (const lap of included) {
        const value = lap[key] as number | null;
        const samples = lap.sampleCount ?? 0;
        if (value === null || samples <= 0) continue;
        total += value * samples;
        weight += samples;
      }
      return weight > 0 ? total / weight : null;
    };

    const times = pick("lapTimeS");
    const maxRpm = pick("maxRpm");
    const minRpm = pick("minRpm");
    const maxTemp = pick("maxTempC");
    const minTemp = pick("minTempC");

    return {
      bestLap: times.length ? Math.min(...times) : null,
      bestLapNumber:
        times.length
          ? (included.find((l) => l.lapTimeS === Math.min(...times))?.lapNumber ?? null)
          : null,
      maxRpm: maxRpm.length ? Math.max(...maxRpm) : null,
      minRpm: minRpm.length ? Math.min(...minRpm) : null,
      avgRpm: weightedMean("avgRpm"),
      maxTemp: maxTemp.length ? Math.max(...maxTemp) : null,
      minTemp: minTemp.length ? Math.min(...minTemp) : null,
      avgTemp: weightedMean("avgTempC"),
    };
  }, [included]);

  const columns = showPowerzone
    ? "3rem 6rem repeat(9, minmax(4.5rem, 1fr))"
    : "3rem 6rem repeat(8, minmax(4.5rem, 1fr))";

  return (
    <div>
      <div className="mb-6 flex flex-wrap items-center gap-4">
        <div className="rounded border border-hairline bg-surface px-3 py-2">
          <div className="text-sm font-semibold text-gain">{driverName}</div>
          <div className="text-[11px] text-muted">
            {sessionDate(startDate)} {sessionTime(startTime)}
            {trackName ? ` · ${trackName}` : ""}
          </div>
        </div>
        {engineCategory && (
          <span className="rounded bg-selected px-2 py-1 text-[9px] font-semibold uppercase tracking-wider text-ink2">
            {engineCategory}
          </span>
        )}
        <div className="ml-auto flex items-center gap-3 text-sm">
          <Link href={`/sessions/${sessionId}`} className="text-muted underline hover:text-ink">
            Lap analysis
          </Link>
          <span className="border-b-2 border-accent pb-0.5 font-semibold">Engine analysis</span>
          <Link href="/" className="text-muted underline hover:text-ink">
            All sessions
          </Link>
        </div>
      </div>

      <div className="mb-6 grid gap-3 sm:grid-cols-2 lg:grid-cols-4 xl:grid-cols-8">
        <Card label="Best lap">
          <span className="font-mono text-xl font-bold">{lapClock(stats.bestLap)}</span>
          <span className="mt-1 block text-[11px] text-muted">
            {stats.bestLapNumber ? `Lap ${stats.bestLapNumber}` : "—"}
          </span>
        </Card>
        <Card label="Total laps">
          <span className="font-mono text-xl font-bold text-theoretical">
            {included.length}/{laps.length}
          </span>
          <span className="mt-1 block text-[11px] text-muted">
            {laps.length - included.length} excl.
          </span>
        </Card>
        <Card label="Max RPM">
          <span className="font-mono text-xl font-bold text-loss">{num(stats.maxRpm)}</span>
        </Card>
        <Card label="Min RPM">
          <span className="font-mono text-xl font-bold text-ink2">{num(stats.minRpm)}</span>
        </Card>
        <Card label="Avg RPM">
          <span className="font-mono text-xl font-bold">{num(stats.avgRpm)}</span>
        </Card>
        <Card label="Max temp">
          <span className="font-mono text-xl font-bold text-loss">{num(stats.maxTemp, 1)}°</span>
        </Card>
        <Card label="Min temp">
          <span className="font-mono text-xl font-bold text-ink2">{num(stats.minTemp, 1)}°</span>
        </Card>
        <Card label="Avg temp">
          <span className="font-mono text-xl font-bold">{num(stats.avgTemp, 1)}°</span>
        </Card>
      </div>

      <div className="overflow-x-auto rounded border border-hairline bg-surface">
        <div className="min-w-[1000px]">
          <div
            className="grid gap-2 border-b border-hairline px-3 py-2"
            style={{ gridTemplateColumns: columns }}
          >
            <span className="label">Lap</span>
            <span className="label text-right">Time</span>
            <span className="label text-right">Max spd</span>
            <span className="label text-right">Min spd</span>
            <span className="label text-right">Max RPM</span>
            <span className="label text-right">Min RPM</span>
            <span className="label text-right">Avg RPM</span>
            <span className="label text-right">Max °C</span>
            <span className="label text-right">Min °C</span>
            <span className="label text-right">Avg °C</span>
            {showPowerzone && <span className="label text-right">Powerzone*</span>}
          </div>

          {laps.length === 0 ? (
            <p className="px-3 py-6 text-sm text-muted">No laps in this session.</p>
          ) : (
            laps.map((lap) => (
              <div
                key={lap.lapNumber}
                className={`grid items-center gap-2 border-b border-hairline/60 px-3 py-1.5 hover:bg-rowalt ${
                  lap.excluded ? "opacity-50" : ""
                }`}
                style={{ gridTemplateColumns: columns }}
              >
                <span className="font-mono text-xs">
                  {lap.lapNumber}
                  {lap.excluded && <span className="ml-1 text-[9px] text-muted">excl</span>}
                </span>
                <span className="text-right font-mono text-xs font-bold">
                  {lapClock(lap.lapTimeS)}
                </span>
                <span className="text-right font-mono text-xs text-ink2">
                  {num(lap.maxSpeedKmh)}
                </span>
                <span className="text-right font-mono text-xs text-muted">
                  {num(lap.minSpeedKmh)}
                </span>
                <span className="text-right font-mono text-xs text-ink2">{num(lap.maxRpm)}</span>
                <span className="text-right font-mono text-xs text-muted">{num(lap.minRpm)}</span>
                <span className="text-right font-mono text-xs">{num(lap.avgRpm)}</span>
                <span className="text-right font-mono text-xs text-ink2">
                  {num(lap.maxTempC, 1)}
                </span>
                <span className="text-right font-mono text-xs text-muted">
                  {num(lap.minTempC, 1)}
                </span>
                <span className="text-right font-mono text-xs">{num(lap.avgTempC, 1)}</span>
                {showPowerzone && (
                  <span className="text-right font-mono text-xs text-gain">
                    {lap.powerzonePct === null ? "—" : `${lap.powerzonePct.toFixed(1)}%`}
                  </span>
                )}
              </div>
            ))
          )}
        </div>
      </div>

      <div className="mt-3 space-y-2 text-xs text-muted">
        {showPowerzone ? (
          <p>
            * <strong className="text-ink2">Powerzone</strong> is the share of the lap spent between{" "}
            {POWERZONE_RPM[0].toLocaleString()} and {POWERZONE_RPM[1].toLocaleString()} rpm &mdash;
            where a Rotax makes its power. Higher means more of the lap was spent in the band the
            engine pulls hardest in, which is mostly a gearing and corner-exit question. Measured
            over logged RPM samples, which arrive at a steady rate, so it closely tracks time.
            Shown because your class is set to {engineCategory}; it is hidden for non-Rotax
            classes, where the band would be the wrong one to read against.
          </p>
        ) : (
          <p>
            Powerzone % is a Rotax figure (time between {POWERZONE_RPM[0].toLocaleString()} and{" "}
            {POWERZONE_RPM[1].toLocaleString()} rpm) and is hidden because your engine class is{" "}
            {engineCategory ? `set to ${engineCategory}` : "not set"}. Change it in{" "}
            <Link href="/settings" className="underline">
              Settings
            </Link>
            .
          </p>
        )}
        <p>
          Temperature is the logger&apos;s engine sensor. Session averages are weighted by each
          lap&apos;s sample count, so a short out-lap does not count as much as a full one.
        </p>
      </div>
    </div>
  );
}

function Card({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="rounded-md border border-hairline bg-raised px-3 py-2">
      <div className="label mb-1">{label}</div>
      {children}
    </div>
  );
}
