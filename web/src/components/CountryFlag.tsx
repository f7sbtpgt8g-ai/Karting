import { countryCode } from "@/lib/flags";

/**
 * A flag icon (from the `flag-icons` package -- see lib/flags.ts for why an
 * image icon rather than an emoji). Renders nothing for an unknown/unset
 * country rather than a placeholder box, matching how the emoji version
 * used to just omit itself.
 */
export function CountryFlag({ country }: { country: string | null | undefined }) {
  const code = countryCode(country);
  if (!code) return null;
  return (
    <span
      className={`fi fi-${code} shrink-0 align-[-0.1em]`}
      role="img"
      aria-label={country ?? undefined}
    />
  );
}

/**
 * A driver's name preceded by their country's flag, for DOM contexts.
 *
 * Not for Plotly hover/legend text or native `<option>` elements -- neither
 * can render the CSS-based icon this uses, and stuffing a flag into that
 * plain-text string is the emoji bug this component exists to avoid
 * repeating. Those call sites pass the plain name straight through instead.
 */
export function DriverName({
  name,
  country,
}: {
  name: string;
  country: string | null | undefined;
}) {
  return (
    <span className="inline-flex items-center gap-1.5">
      <CountryFlag country={country} />
      {name}
    </span>
  );
}
