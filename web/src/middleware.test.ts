import { existsSync } from "fs";
import { join } from "path";
import { describe, expect, it } from "vitest";

/**
 * Where the middleware file sits is load-bearing, and getting it wrong fails
 * silently.
 *
 * Next.js looks for `middleware.ts` beside the `app` directory -- which, in a
 * project using `src/`, means `src/middleware.ts`. Put it at the project root
 * instead and Next simply does not find it: no error, no warning, no line in
 * the build output. The app builds and deploys perfectly, and every route is
 * served with no auth gate and no session refresh.
 *
 * That is exactly what shipped. It went unnoticed because the two things the
 * middleware does are both invisible to someone who is already signed in:
 * redirecting anonymous visitors to /login, and renewing the Supabase access
 * token before it expires. The first person to open the app in a private
 * window saw a page nobody should have reached.
 *
 * A build-output assertion would be closer to the real check, but this is the
 * condition that actually decides it.
 */
const WEB_ROOT = join(__dirname, "..");

describe("middleware placement", () => {
  it("lives inside src/, where Next.js looks for it", () => {
    expect(existsSync(join(WEB_ROOT, "src", "middleware.ts"))).toBe(true);
  });

  it("is not at the project root, where Next.js ignores it", () => {
    expect(existsSync(join(WEB_ROOT, "middleware.ts"))).toBe(false);
  });
});
