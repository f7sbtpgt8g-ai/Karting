"use client";

import { useCallback, useRef, useState } from "react";
import { createClient } from "@/lib/supabase/client";

export const SESSION_TYPES = [
  "Training",
  "Warm up",
  "Free practice",
  "Qualifying",
  "Heat",
  "Superheat",
  "Final",
];
export const CONDITIONS = ["Dry", "Wet", "Mixed"];

type Profile = { id: number; display_name: string };

type Phase = "idle" | "presigning" | "uploading" | "confirming" | "parsing" | "done" | "error";

type BatchStatus = {
  id: number;
  status: string;
  error_message: string | null;
  sessions_created: number | null;
};

async function postJson(url: string, body: unknown) {
  const response = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload?.error || `${url} failed (${response.status})`);
  return payload;
}

export default function UploadForm({ profiles }: { profiles: Profile[] }) {
  const [file, setFile] = useState<File | null>(null);
  const [trackName, setTrackName] = useState("");
  const [sessionType, setSessionType] = useState("");
  const [condition, setCondition] = useState("");
  const [temperature, setTemperature] = useState("");
  const [visibility, setVisibility] = useState("shared");
  const [profileId, setProfileId] = useState<string>(profiles[0] ? String(profiles[0].id) : "");

  const [phase, setPhase] = useState<Phase>("idle");
  const [progress, setProgress] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<BatchStatus | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  const busy = phase !== "idle" && phase !== "done" && phase !== "error";

  /**
   * Poll the batch until the worker finishes with it.
   *
   * Polling rather than a realtime subscription on purpose: this is a single
   * row the user is already staring at, the wait is tens of seconds, and a
   * poll degrades to "refresh the page" if the tab sleeps -- whereas a
   * dropped realtime socket leaves the spinner up forever.
   */
  const waitForWorker = useCallback(async (batchId: number) => {
    const deadline = Date.now() + 10 * 60 * 1000;
    while (Date.now() < deadline) {
      await new Promise((resolve) => setTimeout(resolve, 2000));
      const response = await fetch(`/api/uploads/confirm?batchId=${batchId}`, {
        cache: "no-store",
      });
      if (!response.ok) continue;
      const status: BatchStatus = await response.json();
      if (status.status === "complete" || status.status === "failed") return status;
    }
    throw new Error(
      "The upload is taking longer than expected. It is still queued -- reload this page to check on it.",
    );
  }, []);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    if (!file) return;

    setError(null);
    setResult(null);
    setProgress(0);

    try {
      setPhase("presigning");
      const presigned = await postJson("/api/uploads/presign", { filename: file.name });

      // Straight from the browser to Storage. The bytes never touch a Vercel
      // function -- a real export is tens of MB, past the request body limit.
      setPhase("uploading");
      const supabase = createClient();
      const { error: uploadError } = await supabase.storage
        .from("telemetry")
        .uploadToSignedUrl(presigned.path, presigned.token, file, {
          contentType: "text/tab-separated-values",
        });
      if (uploadError) throw new Error(`Upload failed: ${uploadError.message}`);
      setProgress(100);

      setPhase("confirming");
      const confirmed = await postJson("/api/uploads/confirm", {
        path: presigned.path,
        originalFilename: file.name,
        sizeBytes: file.size,
        driverProfileId: profileId ? Number(profileId) : null,
        trackName: trackName || null,
        sessionType: sessionType || null,
        trackCondition: condition || null,
        temperatureC: temperature ? Number(temperature) : null,
        conditionsSource: temperature || condition ? "manual" : null,
        visibility,
      });

      setPhase("parsing");
      const finished = await waitForWorker(confirmed.batchId);
      setResult(finished);
      if (finished.status === "failed") {
        setPhase("error");
        setError(finished.error_message || "Parsing failed.");
        return;
      }
      setPhase("done");
      setFile(null);
      if (inputRef.current) inputRef.current.value = "";
    } catch (err) {
      setPhase("error");
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  const phaseLabel: Record<Phase, string> = {
    idle: "",
    presigning: "Preparing upload...",
    uploading: "Uploading to storage...",
    confirming: "Queueing for processing...",
    parsing: "Parsing telemetry -- this takes up to a minute for a full track day.",
    done: "",
    error: "",
  };

  return (
    <form onSubmit={submit} className="space-y-6">
      <div>
        <label className="label mb-1 block" htmlFor="file">
          Unipro export (.tsv / .txt)
        </label>
        <input
          id="file"
          ref={inputRef}
          type="file"
          accept=".tsv,.txt,text/plain,text/tab-separated-values"
          required
          onChange={(e) => setFile(e.target.files?.[0] ?? null)}
          disabled={busy}
          className="w-full rounded border border-hairline bg-surface px-3 py-2 text-sm file:mr-3 file:rounded file:border-0 file:bg-raised file:px-3 file:py-1 file:text-sm file:text-ink2"
        />
        {file && (
          <p className="mt-1 font-mono text-xs text-muted">
            {file.name} &middot; {(file.size / 1_048_576).toFixed(1)} MB
          </p>
        )}
      </div>

      <div className="grid gap-4 sm:grid-cols-2">
        <div>
          <label className="label mb-1 block" htmlFor="track">
            Track
          </label>
          <input
            id="track"
            value={trackName}
            onChange={(e) => setTrackName(e.target.value)}
            disabled={busy}
            className="w-full rounded border border-hairline bg-surface px-3 py-2 text-sm outline-none focus:border-accent"
          />
        </div>

        <div>
          <label className="label mb-1 block" htmlFor="type">
            Session type
          </label>
          <select
            id="type"
            value={sessionType}
            onChange={(e) => setSessionType(e.target.value)}
            disabled={busy}
            className="w-full rounded border border-hairline bg-surface px-3 py-2 text-sm outline-none focus:border-accent"
          >
            <option value="">Not specified</option>
            {SESSION_TYPES.map((option) => (
              <option key={option} value={option}>
                {option}
              </option>
            ))}
          </select>
        </div>

        <div>
          <label className="label mb-1 block" htmlFor="condition">
            Conditions
          </label>
          <select
            id="condition"
            value={condition}
            onChange={(e) => setCondition(e.target.value)}
            disabled={busy}
            className="w-full rounded border border-hairline bg-surface px-3 py-2 text-sm outline-none focus:border-accent"
          >
            <option value="">Not specified</option>
            {CONDITIONS.map((option) => (
              <option key={option} value={option}>
                {option}
              </option>
            ))}
          </select>
        </div>

        <div>
          <label className="label mb-1 block" htmlFor="temp">
            Track temperature (&deg;C)
          </label>
          <input
            id="temp"
            type="number"
            step="0.1"
            value={temperature}
            onChange={(e) => setTemperature(e.target.value)}
            disabled={busy}
            className="w-full rounded border border-hairline bg-surface px-3 py-2 text-sm outline-none focus:border-accent"
          />
        </div>

        {profiles.length > 0 && (
          <div>
            <label className="label mb-1 block" htmlFor="driver">
              Driver
            </label>
            <select
              id="driver"
              value={profileId}
              onChange={(e) => setProfileId(e.target.value)}
              disabled={busy}
              className="w-full rounded border border-hairline bg-surface px-3 py-2 text-sm outline-none focus:border-accent"
            >
              {profiles.map((profile) => (
                <option key={profile.id} value={profile.id}>
                  {profile.display_name}
                </option>
              ))}
              <option value="">Decide after parsing</option>
            </select>
          </div>
        )}

        <div>
          <label className="label mb-1 block" htmlFor="visibility">
            Sharing
          </label>
          <select
            id="visibility"
            value={visibility}
            onChange={(e) => setVisibility(e.target.value)}
            disabled={busy}
            className="w-full rounded border border-hairline bg-surface px-3 py-2 text-sm outline-none focus:border-accent"
          >
            <option value="private">Private &mdash; only me</option>
            <option value="team">Team &mdash; my teams</option>
            <option value="shared">Shared &mdash; all drivers</option>
          </select>
        </div>
      </div>

      <button
        type="submit"
        disabled={busy || !file}
        className="rounded bg-accent px-5 py-2 text-sm font-semibold text-white disabled:opacity-40"
      >
        {busy ? "Working..." : "Upload"}
      </button>

      {busy && (
        <div className="space-y-2" role="status">
          <p className="text-sm text-ink2">{phaseLabel[phase]}</p>
          <div className="h-1 w-full overflow-hidden rounded bg-raised">
            <div
              className="h-full bg-accent transition-all"
              style={{ width: phase === "parsing" ? "100%" : `${Math.max(progress, 15)}%` }}
            />
          </div>
        </div>
      )}

      {phase === "done" && result && (
        <p className="text-sm text-gain" role="status">
          {result.sessions_created === 0
            ? "That file was already in your library -- nothing new to add."
            : `Stored ${result.sessions_created} session${result.sessions_created === 1 ? "" : "s"}.`}
        </p>
      )}

      {error && (
        <p className="text-sm text-loss" role="alert">
          {error}
        </p>
      )}
    </form>
  );
}
