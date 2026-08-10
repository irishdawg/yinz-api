import { callGotiateApi } from "@/lib/gotiate-api";

export async function POST(request: Request) {
  const handlerStart = Date.now();
  const body = await request.json();
  const { response, requestId, elapsedMs } = await callGotiateApi(request, "/games", {
    method: "POST",
    body: JSON.stringify(body),
  });
  const data = await response.json();
  const totalMs = Date.now() - handlerStart;

  console.log(
    JSON.stringify({
      event: "route_handler_complete",
      request_id: requestId,
      path: "/api/games",
      status: response.status,
      fetch_ms: elapsedMs,
      total_ms: totalMs,
      vercel_overhead_ms: totalMs - elapsedMs,
    }),
  );

  return Response.json(data, { status: response.status, headers: { "X-Request-ID": requestId } });
}
