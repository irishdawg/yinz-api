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

/** Thin wrapper around POST /api/games/[id]/commands -- the one path every
 * gameplay action goes through (SET_EXPECTED_PLAYER_COUNT, START_GAME,
 * PROPOSE_SWAP, ...). Generates the idempotency key so callers never have
 * to; `onSettled` fires after the response lands (success *or* a handled
 * 409) so the caller's game-view poll refreshes immediately instead of
 * waiting for its next tick -- the acting player's own action should feel
 * instant regardless of the poll interval. */
export async function submitCommand(
  gameId: string,
  type: string,
  payload: Record<string, unknown> = {},
  options: { expectedVersion?: number | null; onSettled?: () => void } = {},
): Promise<CommandResult> {
  try {
    const response = await fetch(`/api/games/${gameId}/commands`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        command_id: crypto.randomUUID(),
        type,
        expected_version: options.expectedVersion ?? null,
        payload,
      }),
    });
    const data = await response.json();
    return { ok: response.ok, status: response.status, data };
  } finally {
    options.onSettled?.();
  }
}
