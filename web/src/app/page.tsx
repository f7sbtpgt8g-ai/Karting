import { redirect } from "next/navigation";

// Only the ingest path exists so far; the analysis screens are still the
// Streamlit app's. Send everyone straight to it rather than shipping a
// landing page that links to pages that do not exist yet.
export default function Home() {
  redirect("/upload");
}
