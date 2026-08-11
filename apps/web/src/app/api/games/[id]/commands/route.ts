import { callGotiateApi } from "@/lib/gotiate-api";

export async function POST(request: Request, { params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  const body = await request.json();
  const { response } = await callGotiateApi(request, `/games/${id}/commands`, {
    method: "POST",
    body: JSON.stringify(body),
  });
  const data = await response.json();
  return Response.json(data, { status: response.status });
}
