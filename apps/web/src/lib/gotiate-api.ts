import { createClient } from "@/lib/supabase/server";

const API_URL = process.env.GOTIATE_API_URL!;
const GATEWAY_SECRET = process.env.GOTIATE_GATEWAY_SECRET!;

/** Server-side only -- called from Route Handlers, never from a client
 * component. Forwards the caller's real Supabase session as a bearer
 * token and the shared gateway secret FastAPI requires on every request
 * (the browser never calls FastAPI directly). Also forwards the real
 * client IP the incoming request arrived with (as Vercel's edge set it),
 * separately from anything FastAPI itself sees as the TCP peer, which
 * once deployed will always be Vercel's own egress IP -- see
 * apps/api/src/gotiate/api/rate_limit.py for why that distinction
 * matters for rate limiting. */
export async function callGotiateApi(incomingRequest: Request, path: string, init: RequestInit = {}): Promise<Response> {
  const supabase = await createClient();
  const {
    data: { session },
  } = await supabase.auth.getSession();
  if (!session) {
    return Response.json({ error: "unauthorized" }, { status: 401 });
  }

  const clientIp = incomingRequest.headers.get("x-forwarded-for")?.split(",")[0]?.trim();

  const headers = new Headers(init.headers);
  headers.set("Authorization", `Bearer ${session.access_token}`);
  headers.set("X-Gotiate-Gateway-Key", GATEWAY_SECRET);
  headers.set("Content-Type", "application/json");
  if (clientIp) headers.set("X-Gotiate-Client-IP", clientIp);

  return fetch(`${API_URL}${path}`, { ...init, headers });
}
