import { NextResponse } from "next/server";
import { createClient, getAppUser } from "@/lib/supabase/server";

const VISIBILITY = ["private", "team", "shared"] as const;
type Visibility = (typeof VISIBILITY)[number];

/**
 * POST /api/uploads/confirm
 *
 * Called once the browser's direct-to-Storage upload succeeds. Enqueues the
 * file for the background worker by inserting an `upload_batches` row with
 * status='pending', and returns its id so the client can poll for progress.
 *
 * Everything the parser cannot infer from the file itself is captured here
 * and carried onto every session the upload produces: track name, session
 * type, conditions, the sharing tier, and who the sessions belong to.
 *
 * The insert runs as the caller under RLS. `upload_batches_insert_own` (0003)
 * pins `uploaded_by_user_id` to the caller and refuses any status other than
 * 'pending', so a client cannot enqueue work as somebody else, nor mark an
 * unparsed file complete.
 */
export async function POST(request: Request) {
  const supabase = await createClient();
  const appUser = await getAppUser();

  if (!appUser) {
    return NextResponse.json({ error: "Not signed in." }, { status: 401 });
  }

  let body: Record<string, unknown>;
  try {
    body = await request.json();
  } catch {
    return NextResponse.json({ error: "Expected a JSON body." }, { status: 400 });
  }

  const storagePath = typeof body.path === "string" ? body.path : "";
  if (!storagePath) {
    return NextResponse.json({ error: "Missing the uploaded file's path." }, { status: 400 });
  }

  // The storage policy already confines writes to the caller's own folder,
  // but /confirm is a separate request and could name any path -- so check
  // the claim here too rather than trusting it to match what /presign issued.
  if (!storagePath.startsWith(`${appUser.authId}/`)) {
    return NextResponse.json(
      { error: "That upload path does not belong to this account." },
      { status: 403 },
    );
  }

  const visibility: Visibility = VISIBILITY.includes(body.visibility as Visibility)
    ? (body.visibility as Visibility)
    : "shared";

  const numeric = (value: unknown) =>
    typeof value === "number" && Number.isFinite(value) ? value : null;
  const text = (value: unknown) =>
    typeof value === "string" && value.trim() ? value.trim() : null;

  const { data, error } = await supabase
    .from("upload_batches")
    .insert({
      storage_path: storagePath,
      original_filename: text(body.originalFilename),
      size_bytes: numeric(body.sizeBytes),
      uploaded_by_user_id: appUser.id,
      // Null means "ask who drove each session after parsing" -- a single
      // logger file can legitimately hold several drivers' runs.
      driver_profile_id: numeric(body.driverProfileId),
      track_name: text(body.trackName),
      session_type: text(body.sessionType),
      track_condition: text(body.trackCondition),
      temperature_c: numeric(body.temperatureC),
      humidity_pct: numeric(body.humidityPct),
      pressure_hpa: numeric(body.pressureHpa),
      altitude_m: numeric(body.altitudeM),
      conditions_source: text(body.conditionsSource),
      visibility,
      status: "pending",
    })
    .select("id, status")
    .single();

  if (error) {
    return NextResponse.json(
      { error: `Could not queue the upload: ${error.message}` },
      { status: 500 },
    );
  }

  return NextResponse.json({ batchId: data.id, status: data.status });
}

/**
 * GET /api/uploads/confirm?batchId=123 -- progress for one batch.
 *
 * Polled by the upload page while the worker parses. RLS restricts this to
 * the caller's own batches, so the id needs no further checking here.
 */
export async function GET(request: Request) {
  const supabase = await createClient();
  const batchId = new URL(request.url).searchParams.get("batchId");
  if (!batchId) {
    return NextResponse.json({ error: "Missing batchId." }, { status: 400 });
  }

  const { data, error } = await supabase
    .from("upload_batches")
    .select("id, status, error_message, sessions_created, created_at, finished_at")
    .eq("id", batchId)
    .maybeSingle();

  if (error) {
    return NextResponse.json({ error: error.message }, { status: 500 });
  }
  if (!data) {
    return NextResponse.json({ error: "No such upload." }, { status: 404 });
  }
  return NextResponse.json(data);
}
