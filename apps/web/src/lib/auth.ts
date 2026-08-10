import { createClient } from "@/lib/supabase/client";

/** Checks for an existing Supabase session, silently signing in
 * anonymously if there isn't one. Callers must await this before
 * submitting anything that hits our API routes -- a submit that races the
 * session-cookie write will reach the Route Handler with no session. */
export async function ensureAnonymousSession(): Promise<void> {
  const supabase = createClient();
  const {
    data: { session },
  } = await supabase.auth.getSession();
  if (session) return;

  const { error } = await supabase.auth.signInAnonymously();
  if (error) throw error;
}
