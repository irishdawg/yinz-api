import { callGotiateApi } from "@/lib/gotiate-api";

export async function POST(request: Request) {
  const body = await request.json();
  const response = await callGotiateApi(request, "/games/join", {
    method: "POST",
    body: JSON.stringify(body),
  });
  const data = await response.json();
  return Response.json(data, { status: response.status });
}
