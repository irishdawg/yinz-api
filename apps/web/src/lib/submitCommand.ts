export interface CommandErrorDetail {
  error_code: string;
  message: string;
}

/** Success bodies are a full game-view dict (or `{status, resulting_version}`
 * for an idempotent replay) -- shape varies by command, so this stays loose
 * on purpose. Error bodies always carry `detail`, either FastAPI's plain
 * string form or our structured `{error_code, message}` form. */
export type CommandResponseData = { detail?: CommandErrorDetail | string } & Record<string, unknown>;

export interface CommandResult {
  ok: boolean;
  status: number;
  data: CommandResponseData;
}

export function commandErrorMessage(data: CommandResponseData, fallback: string): string {
  const detail = data.detail;
  if (typeof detail === "string") return detail;
  if (detail?.message) return detail.message;
  return fallback;
}

async function postCommand(gameId: string, type: string, payload: Record<string, unknown>, expectedVersion: number | null): Promise<CommandResult> {
  const response = await fetch(`/api/games/${gameId}/commands`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ command_id: crypto.randomUUID(), type, expected_version: expectedVersion, payload }),
  });
  const data = await response.json();
  return { ok: response.ok, status: response.status, data };
}

/** Thin wrapper around POST /api/games/[id]/commands -- the one path every
 * gameplay action goes through (SET_EXPECTED_PLAYER_COUNT, START_GAME,
 * PROPOSE_SWAP, ...). Generates the idempotency key so callers never have
 * to; `onSettled` fires after the response lands (success *or* a handled
 * 409) so the caller's game-view poll refreshes immediately instead of
 * waiting for its next tick -- the acting player's own action should feel
 * instant regardless of the poll interval.
 *
 * A `stale_version` 409 usually just means another player's action landed
 * a beat before this poll caught up -- not a real problem, and not
 * something a player should see as a scary red error. On that specific
 * error code (and only that one) we silently re-fetch the current version
 * and retry the exact same command once; any other failure (including a
 * *second* stale_version, or the retry being illegal against the new
 * state) surfaces to the caller normally. */
export async function submitCommand(
  gameId: string,
  type: string,
  payload: Record<string, unknown> = {},
  options: { expectedVersion?: number | null; onSettled?: () => void } = {},
): Promise<CommandResult> {
  try {
    const first = await postCommand(gameId, type, payload, options.expectedVersion ?? null);
    if (first.ok || first.status !== 409) return first;
    const detail = first.data.detail;
    const errorCode = typeof detail === "string" ? undefined : detail?.error_code;
    if (errorCode !== "stale_version") return first;

    const freshView = await fetch(`/api/games/${gameId}`).then((r) => (r.ok ? r.json() : null));
    const freshVersion = typeof freshView?.version === "number" ? freshView.version : null;
    if (freshVersion === null) return first;

    return await postCommand(gameId, type, payload, freshVersion);
  } finally {
    options.onSettled?.();
  }
}
