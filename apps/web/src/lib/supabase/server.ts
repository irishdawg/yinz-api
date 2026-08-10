import { createServerClient } from "@supabase/ssr";
import { cookies } from "next/headers";

// Used inside Route Handlers (src/app/api/**) to read the session cookie
// createClient() (client.ts) wrote in the browser, so we can forward the
// user's real access token to FastAPI without the browser ever calling it
// directly.
export async function createClient() {
  const cookieStore = await cookies();

  return createServerClient(
    process.env.NEXT_PUBLIC_SUPABASE_URL!,
    process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY!,
    {
      cookies: {
        getAll() {
          return cookieStore.getAll();
        },
        setAll(cookiesToSet) {
          try {
            cookiesToSet.forEach(({ name, value, options }) => cookieStore.set(name, value, options));
          } catch {
            // Route Handlers can set cookies; Server Components can't.
            // Harmless no-op when called from the latter.
          }
        },
      },
    },
  );
}
