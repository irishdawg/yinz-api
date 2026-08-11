import { callGotiateApi } from "@/lib/gotiate-api";

export async function GET(request: Request, { params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  const { response } = await callGotiateApi(request, `/games/${id}`);
  const data = await response.json();
  return Response.json(data, { status: response.status });
}
