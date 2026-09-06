import Link from "next/link";
import SignOutButton from "./SignOutButton";

/**
 * The top bar, matching the Streamlit app's own top-nav layout.
 *
 * Only the pages that actually exist are listed. The rest of the parity
 * set still lives in Streamlit, and a link to a route that 404s reads as a
 * broken app rather than an unfinished one.
 */
export default function AppHeader({ email, current }: { email?: string | null; current: string }) {
  const links = [
    { href: "/", label: "Home" },
    { href: "/upload", label: "Upload" },
    { href: "/settings", label: "Settings" },
  ];

  return (
    <header className="mb-8 flex flex-wrap items-center justify-between gap-4 border-b border-hairline pb-3">
      <div className="flex items-center gap-6">
        <Link href="/" className="flex items-center gap-3">
          <div className="h-4 w-3.5 -skew-x-12 bg-accent" />
          <span className="text-sm font-bold uppercase tracking-[0.14em]">Karting Telemetry</span>
        </Link>
        <nav className="flex gap-4 text-sm">
          {links.map((link) => (
            <Link
              key={link.href}
              href={link.href}
              className={
                current === link.href
                  ? "border-b-2 border-accent pb-1 font-semibold text-ink"
                  : "border-b-2 border-transparent pb-1 text-muted hover:text-ink"
              }
            >
              {link.label}
            </Link>
          ))}
        </nav>
      </div>
      <div className="flex items-center gap-4">
        {email && <span className="font-mono text-xs text-muted">{email}</span>}
        <SignOutButton />
      </div>
    </header>
  );
}
