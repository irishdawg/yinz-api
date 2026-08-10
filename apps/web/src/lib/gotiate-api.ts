import { createClient } from "@/lib/supabase/server";

const API_URL = process.env.GOTIATE_API_URL!;
const GATEWAY_SECRET = process.env.GOTIATE_GATEWAY_SECRET!;

export interface GotiateApiResult {
  response: Response;
  requestId: string;
  /** Time spent in the fetch() to FastAPI specifically -- network +
   * FastAPI's own processing, not this handler's other work (session
   * lookup, JSON parsing). Callers subtract this from their own total
   * handler time to get a crude read on Vercel-side overhead vs.
   * network+FastAPI time. */
  elapsedMs: number;
}

/** Server-side only -- called from Route Handlers, never from a client
 * component. Forwards the caller's real Supabase session as a bearer
 * token and the shared gateway secret FastAPI requires on every request
 * (the browser never calls FastAPI directly). Also forwards the real
 * client IP the incoming request arrived with (as Vercel's edge set it),
 * separately from anything FastAPI itself sees as the TCP peer, which
 * once deployed will always be Vercel's own egress IP -- see
 * apps/api/src/gotiate/api/rate_limit.py for why that distinction
 * matters for rate limiting.
 *
 * X-Request-ID is generated here and sent to FastAPI, which echoes it
 * back and includes it in its own logs -- the same id shows up in both
 * services' logs for one command, so a specific slow/broken request can
 * be traced end to end instead of guessed at. */
export async function callGotiateApi(incomingRequest: Request, path: string, init: RequestInit = {}): Promise<GotiateApiResult> {
  const requestId = crypto.randomUUID();
  const supabase = await createClient();
  const {
    data: { session },
  } = await supabase.auth.getSession();
  if (!session) {
    return { response: Response.json({ error: "unauthorized" }, { status: 401 }), requestId, elapsedMs: 0 };
  }

  const clientIp = incomingRequest.headers.get("x-forwarded-for")?.split(",")[0]?.trim();

  const headers = new Headers(init.headers);
  headers.set("Authorization", `Bearer ${session.access_token}`);
  headers.set("X-Gotiate-Gateway-Key", GATEWAY_SECRET);
  headers.set("Content-Type", "application/json");
  headers.set("X-Request-ID", requestId);
  if (clientIp) headers.set("X-Gotiate-Client-IP", clientIp);

  const start = Date.now();
  const response = await fetch(`${API_URL}${path}`, { ...init, headers });
  const elapsedMs = Date.now() - start;

  console.log(
    JSON.stringify({
      event: "gotiate_api_call",
      request_id: requestId,
      path,
      status: response.status,
      vercel_to_render_ms: elapsedMs,
    }),
  );

  return { response, requestId, elapsedMs };
}
