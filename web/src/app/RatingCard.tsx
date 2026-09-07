"use client";

import { useState } from "react";
import { deriveDisplayedRating, type DriverRatingRow, type RatingHistoryRow } from "@/lib/rating";

/**
 * The Driver Rating card -- the competitive, skill-based number, shown with
 * its provisional status and a "why did it move" breakdown per the
 * explainability requirement. Deliberately its own card, not folded into
 * `ActivityCard` -- the two layers (competitive vs. motivational) stay
 * visually as separate as they are architecturally.
 */
export default function RatingCard({
  rating,
  history,
}: {
  rating: DriverRatingRow | null;
  history: RatingHistoryRow[];
}) {
  const [expanded, setExpanded] = useState(false);

  if (!rating) {
    return (
      <div className="rounded border border-hairline bg-surface p-4">
        <div className="label mb-1">Driver Rating</div>
        <p className="text-sm text-muted">
          Not yet rated -- a verified lap from a session compared against other drivers (or, with no field
          that day, against a track's own history) is what starts this number moving.
        </p>
      </div>
    );
  }

  const { displayedRating, sigmaEffective, provisional } = deriveDisplayedRating(rating);

  return (
    <div className="rounded border border-hairline bg-surface p-4">
      <div className="mb-1 flex items-center gap-2">
        <div className="label">Driver Rating</div>
        {provisional && (
          <span className="rounded bg-theoretical/20 px-1.5 py-0.5 text-[9px] font-semibold uppercase tracking-wider text-theoretical">
            Provisional
          </span>
        )}
      </div>
      <div className="font-mono text-3xl font-bold">{Math.round(displayedRating)}</div>
      <p className="mt-1 text-[11px] text-muted">
        {provisional
          ? "Still settling -- more verified sessions will narrow this."
          : `Confidence σ≈${Math.round(sigmaEffective)}, ${rating.sessionsRatedCount} session${
              rating.sessionsRatedCount === 1 ? "" : "s"
            } rated.`}
      </p>

      {history.length > 0 && (
        <div className="mt-3 border-t border-hairline pt-2">
          <button
            type="button"
            onClick={() => setExpanded((v) => !v)}
            className="text-xs text-muted underline"
          >
            {expanded ? "Hide" : "Why did this move?"}
          </button>
          {expanded && (
            <ul className="mt-2 space-y-2">
              {history.map((h) => {
                const delta = h.muAfter - h.muBefore;
                return (
                  <li key={h.id} className="text-xs">
                    <div className="flex items-center justify-between">
                      <span className="text-muted">
                        {h.cohortDate ? new Date(h.cohortDate).toLocaleDateString() : "—"}
                        {h.cohortTrack ? ` · ${h.cohortTrack}` : ""}
                      </span>
                      <span className={`font-mono font-semibold ${delta >= 0 ? "text-gain" : "text-loss"}`}>
                        {delta >= 0 ? "+" : ""}
                        {delta.toFixed(1)}
                      </span>
                    </div>
                    <div className="text-[11px] text-muted">
                      {h.mechanism === "field"
                        ? `Field-based -- compared against ${h.cohortSize - 1} other driver${
                            h.cohortSize - 1 === 1 ? "" : "s"
                          }${h.cohortConditions ? ` (${h.cohortConditions})` : ""}.`
                        : "Reference-based -- no comparable field that day, scored against the track's history."}
                      {h.note ? ` ${h.note}` : ""}
                    </div>
                  </li>
                );
              })}
            </ul>
          )}
        </div>
      )}
    </div>
  );
}
