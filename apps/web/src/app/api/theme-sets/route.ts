import { callGotiateApi } from "@/lib/gotiate-api";

export async function GET(request: Request) {
  const { response } = await callGotiateApi(request, "/theme-sets");
  const data = await response.json();
  return Response.json(data, { status: response.status });
}
