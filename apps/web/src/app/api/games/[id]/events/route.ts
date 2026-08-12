import { callGotiateApi } from "@/lib/gotiate-api";

export async function GET(request: Request, { params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  const sinceSeq = new URL(request.url).searchParams.get("since_seq");
  const path = sinceSeq ? `/games/${id}/events?since_seq=${sinceSeq}` : `/games/${id}/events`;
  const { response } = await callGotiateApi(request, path);
  const data = await response.json();
  return Response.json(data, { status: response.status });
}
