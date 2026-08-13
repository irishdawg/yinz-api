import { callGotiateApi } from "@/lib/gotiate-api";

export async function GET(request: Request, { params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  const url = new URL(request.url);
  const entityA = url.searchParams.get("entity_a") ?? "";
  const entityB = url.searchParams.get("entity_b") ?? "";
  const query = new URLSearchParams({ entity_a: entityA, entity_b: entityB });
  const { response } = await callGotiateApi(request, `/games/${id}/propose-cost?${query.toString()}`);
  const data = await response.json();
  return Response.json(data, { status: response.status });
}
