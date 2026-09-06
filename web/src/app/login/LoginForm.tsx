"use client";

import { useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { createClient } from "@/lib/supabase/client";

type Mode = "signin" | "signup" | "reset";

export default function LoginForm() {
  const router = useRouter();
  const params = useSearchParams();
  const next = params.get("next") || "/upload";

  const [mode, setMode] = useState<Mode>("signin");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<{ kind: "error" | "info"; text: string } | null>(null);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setMessage(null);
    const supabase = createClient();

    try {
      if (mode === "signin") {
        const { error } = await supabase.auth.signInWithPassword({ email, password });
        if (error) throw error;
        router.push(next);
        router.refresh();
        return;
      }

      if (mode === "signup") {
        const { error } = await supabase.auth.signUp({
          email,
          password,
          // Carried into the Supabase user's metadata so the display name
          // survives to whatever mirrors the account into `users` locally.
          options: { data: { display_name: displayName || email } },
        });
        if (error) throw error;
        setMessage({
          kind: "info",
          text: "Account created. If this project requires email confirmation, check your inbox before signing in.",
        });
        setMode("signin");
        return;
      }

      const { error } = await supabase.auth.resetPasswordForEmail(email, {
        redirectTo: `${window.location.origin}/login`,
      });
      if (error) throw error;
      // Deliberately the same message whether or not the address exists --
      // confirming which emails are registered would let anyone enumerate
      // accounts, the same reasoning the Streamlit app's reset flow uses.
      setMessage({ kind: "info", text: "If that address has an account, a reset link is on its way." });
    } catch (err) {
      setMessage({ kind: "error", text: err instanceof Error ? err.message : String(err) });
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="mx-auto flex min-h-screen max-w-md flex-col justify-center px-6">
      <div className="mb-8 flex items-center gap-3">
        <div className="h-4 w-3.5 -skew-x-12 bg-accent" />
        <h1 className="text-sm font-bold uppercase tracking-[0.14em]">Karting Telemetry</h1>
      </div>

      <div className="mb-6 flex gap-4 border-b border-hairline text-sm">
        {(
          [
            ["signin", "Sign in"],
            ["signup", "Create account"],
            ["reset", "Forgot password"],
          ] as const
        ).map(([value, label]) => (
          <button
            key={value}
            type="button"
            onClick={() => {
              setMode(value);
              setMessage(null);
            }}
            className={`-mb-px border-b-2 pb-2 ${
              mode === value ? "border-accent text-ink" : "border-transparent text-muted"
            }`}
          >
            {label}
          </button>
        ))}
      </div>

      <form onSubmit={submit} className="space-y-4">
        <div>
          <label className="label mb-1 block" htmlFor="email">
            Email
          </label>
          <input
            id="email"
            type="email"
            required
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            className="w-full rounded border border-hairline bg-surface px-3 py-2 text-sm outline-none focus:border-accent"
          />
        </div>

        {mode === "signup" && (
          <div>
            <label className="label mb-1 block" htmlFor="name">
              Driver name
            </label>
            <input
              id="name"
              value={displayName}
              onChange={(e) => setDisplayName(e.target.value)}
              placeholder="How you appear to other drivers"
              className="w-full rounded border border-hairline bg-surface px-3 py-2 text-sm outline-none focus:border-accent"
            />
          </div>
        )}

        {mode !== "reset" && (
          <div>
            <label className="label mb-1 block" htmlFor="password">
              Password
            </label>
            <input
              id="password"
              type="password"
              required
              minLength={8}
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              className="w-full rounded border border-hairline bg-surface px-3 py-2 text-sm outline-none focus:border-accent"
            />
          </div>
        )}

        <button
          type="submit"
          disabled={busy}
          className="w-full rounded bg-accent py-2 text-sm font-semibold text-white disabled:opacity-50"
        >
          {busy
            ? "Working..."
            : mode === "signin"
              ? "Sign in"
              : mode === "signup"
                ? "Create account"
                : "Send reset link"}
        </button>
      </form>

      {message && (
        <p
          className={`mt-4 text-sm ${message.kind === "error" ? "text-loss" : "text-gain"}`}
          role="status"
        >
          {message.text}
        </p>
      )}
    </main>
  );
}
