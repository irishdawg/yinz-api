/** Pure client-side math over a revealed HaircutProfile -- see the
 * Haircut-risk design writeup. Once revealed, depth_probabilities is
 * fully public with no hidden dependency, so certainty is computed here
 * rather than fetched per-position, same precedent as supportMarkers.ts. */

/** A market position (1-indexed) survives iff the realized depth is
 * strictly less than it -- certainty(p) = sum(depthProbabilities[0:p]),
 * the cumulative probability mass at depths 0..p-1. Checked against the
 * design's worked example: positions 1-6 of [0.48, 0.19, 0.14, 0.11,
 * 0.06, 0.02] give 48/67/81/92/98/100%. */
export function certaintyAt(position: number, depthProbabilities: number[]): number {
  return depthProbabilities.slice(0, position).reduce((sum, p) => sum + p, 0);
}

/** Highest depth with nonzero probability -- the boundary beyond which a
 * position is structurally safe. Used to derive safe_value without a
 * certainty === 1 floating-point comparison. */
export function maxHaircutDepth(depthProbabilities: number[]): number {
  let max = 0;
  for (let i = 0; i < depthProbabilities.length; i++) {
    if (depthProbabilities[i] > 0) max = i;
  }
  return max;
}

/** A position is safe (cannot possibly be wiped) once it's strictly past
 * the profile's max possible depth. */
export function isSafePosition(position: number, depthProbabilities: number[]): boolean {
  return position > maxHaircutDepth(depthProbabilities);
}
