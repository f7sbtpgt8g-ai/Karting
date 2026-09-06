"use client";

import { createBrowserClient } from "@supabase/ssr";
import { supabaseAnonKey, supabaseUrl } from "./env";

/**
 * Browser Supabase client. Both values it reads are designed to be public --
 * the URL and the publishable/anon key ship inside the bundle by design, and
 * are safe there only because Row Level Security actually holds (see
 * supabase/migrations/0002_rls_hardening.sql and tests/test_rls_policies.py).
 */
export function createClient() {
  return createBrowserClient(supabaseUrl(), supabaseAnonKey());
}
