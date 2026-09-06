import { NextResponse } from "next/server";
import { randomUUID } from "crypto";
import { createClient, getAppUser } from "@/lib/supabase/server";

/**
 * POST /api/uploads/presign
 *
 * Hands back a short-lived signed URL the browser PUTs the raw file straight
 * to, plus the storage path to quote back on /confirm.
 *
 * The file never passes through this function. A real Unipro export is tens
 * of megabytes and ~900k rows, comfortably past Vercel's serverless request
 * body limit -- and even under the limit, streaming it through a function
 * would just add a hop and a timeout risk to a transfer Storage handles
 * natively.
 *
 * The server, not the client, chooses the path: `<auth uid>/<uuid>.tsv`. The
 * storage policy in 0003 enforces that the first segment is the caller's own
 * uid, so a client that invented its own path could not write outside its
 * folder anyway -- but generating it here means the client never has to be
 * trusted with the shape at all.
 */
export async function POST(request: Request) {
  const supabase = await createClient();
  const {
    data: { user },
  } = await supabase.auth.getUser();

  if (!user) {
    return NextResponse.json({ error: "Not signed in." }, { status: 401 });
  }

  // An account that authenticates but has no local mirror row cannot own an
  // upload -- and, more importantly, is invisible to every RLS policy. Fail
  // here with something actionable rather than letting /confirm insert a row
  // that silently belongs to nobody.
  const appUser = await getAppUser();
  if (!appUser) {
    return NextResponse.json(
      {
        error:
          "This account isn't linked to a driver profile yet. Sign out and sign in again to finish setting it up.",
      },
      { status: 409 },
    );
  }

  let filename = "upload.tsv";
  try {
    const body = await request.json();
    if (typeof body?.filename === "string" && body.filename.trim()) {
      filename = body.filename.trim();
    }
  } catch {
    // No body is fine -- the filename is only used to pick an extension.
  }

  const extension = filename.toLowerCase().endsWith(".txt") ? "txt" : "tsv";
  const path = `${user.id}/${randomUUID()}.${extension}`;

  const { data, error } = await supabase.storage
    .from("telemetry")
    .createSignedUploadUrl(path);

  if (error) {
    return NextResponse.json(
      { error: `Could not prepare the upload: ${error.message}` },
      { status: 500 },
    );
  }

  return NextResponse.json({
    path,
    token: data.token,
    signedUrl: data.signedUrl,
    originalFilename: filename,
  });
}
