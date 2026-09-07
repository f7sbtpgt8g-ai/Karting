/**
 * Telling a write that changed nothing apart from one that worked.
 *
 * PostgREST reports a row that RLS filtered out of an UPDATE as neither an
 * error nor a rejection: the request succeeds and simply changes nothing.
 * So `error === null` does not mean "saved", and a table updated from what
 * was *asked for* rather than from what came *back* will show values the
 * database never stored -- convincingly, until the next page load drops
 * them.
 *
 * The fix is to ask for the affected rows (`.select()`) and believe only
 * those. This is where that comparison lives, kept separate from the
 * component so it can be tested without a browser.
 */

export type BulkOutcome = {
  /** Ids the database confirmed it wrote. Only these may be shown as saved. */
  saved: Set<number>;
  /** Ids it silently declined. Worth keeping selected so they can be retried. */
  refused: number[];
  /** What to tell the driver, or null when everything landed. */
  message: string | null;
};

/**
 * @param requested ids the update was sent for
 * @param returned  ids the database said it actually updated
 */
export function bulkOutcome(requested: number[], returned: number[]): BulkOutcome {
  const saved = new Set(returned.filter((id) => requested.includes(id)));
  const refused = requested.filter((id) => !saved.has(id));

  if (refused.length === 0) return { saved, refused, message: null };

  // Worth being plain about the count: "some of them didn't save" leaves a
  // driver re-checking thirty rows by hand.
  const message =
    saved.size === 0
      ? `None of the ${requested.length} selected session${
          requested.length === 1 ? " was" : "s were"
        } renamed: the database did not accept the change. They are still selected.`
      : `${refused.length} of ${requested.length} sessions were not renamed: the database ` +
        "did not accept the change for them. They are still selected, so you can try again.";

  return { saved, refused, message };
}
