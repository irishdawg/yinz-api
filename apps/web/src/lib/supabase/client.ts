import { createBrowserClient } from "@supabase/ssr";

// Session lives in cookies, not localStorage -- that's what lets the
// same-origin server-side Route Handlers (src/app/api/**) read it back via
// next/headers cookies() and forward it to FastAPI.
export function createClient() {
  return createBrowserClient(
    process.env.NEXT_PUBLIC_SUPABASE_URL!,
    process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY!,
  );
}
