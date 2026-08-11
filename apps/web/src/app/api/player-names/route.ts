import { callGotiateApi } from "@/lib/gotiate-api";

/** No free-text display names anywhere in the product -- this is the only
 * way the UI gets a name to show. `exclude` present means "reroll" (never
 * golden); absent means "initial offer" (golden-eligible, rate-limited
 * server-side). `join_code`, when present, tells FastAPI to also exclude
 * every name already held by a seated player in that game. */
export async function GET(request: Request) {
  const url = new URL(request.url);
  const exclude = url.searchParams.get("exclude");
  const joinCode = url.searchParams.get("join_code");

  const params = new URLSearchParams();
  if (joinCode) params.set("join_code", joinCode);

  let path: string;
  if (exclude) {
    params.set("exclude", exclude);
    path = `/player-names/reroll?${params.toString()}`;
  } else {
    path = `/player-names/initial?${params.toString()}`;
  }

  const { response } = await callGotiateApi(request, path);
  const data = await response.json();
  return Response.json(data, { status: response.status });
}
