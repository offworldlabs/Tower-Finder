const MAX_TRAIL_POINTS = 400;

export function mergeTrailPositions(existing = [], incoming = []) {
  if (!incoming.length) return existing;

  const normalizedIncoming = incoming
    .filter((point) => Array.isArray(point) && point.length >= 2)
    .slice()
    .sort((a, b) => (a[3] || 0) - (b[3] || 0));

  if (!existing.length) return normalizedIncoming.slice(-MAX_TRAIL_POINTS);

  const merged = [...existing];
  let last = merged[merged.length - 1];
  let lastTs = last?.[3] || 0;

  for (const point of normalizedIncoming) {
    const pointTs = point[3] || 0;

    if (pointTs <= lastTs) continue;

    if (
      last &&
      Math.abs(last[0] - point[0]) < 0.00001 &&
      Math.abs(last[1] - point[1]) < 0.00001
    ) {
      lastTs = pointTs;
      continue;
    }

    merged.push(point);
    last = point;
    lastTs = pointTs;
  }

  // Cap trail length — keep most recent points
  return merged.length > MAX_TRAIL_POINTS ? merged.slice(-MAX_TRAIL_POINTS) : merged;
}

export function sampleTrailPositions(positions, maxPoints = 240) {
  if (!Array.isArray(positions) || positions.length <= maxPoints) return positions || [];

  const stride = Math.ceil(positions.length / maxPoints);
  const sampled = positions.filter((_, index) => index % stride === 0);
  const last = positions[positions.length - 1];
  const tail = sampled[sampled.length - 1];

  if (!tail || tail[0] !== last[0] || tail[1] !== last[1]) {
    sampled.push(last);
  }

  return sampled;
}

export function buildTrailSegments(positions, numSegments = 8) {
  if (!positions || positions.length < 2) return [];
  const segs = [];
  const total = positions.length;
  const step = Math.max(1, Math.ceil(total / numSegments));
  for (let i = 0; i < numSegments; i++) {
    const start = i * step;
    const end = Math.min(start + step + 1, total);
    if (end - start < 2) break;
    const t = (i + 1) / numSegments;
    segs.push({
      positions: positions.slice(start, end),
      opacity: 0.08 + t * 0.92,
      weight: 1.5 + t * 3.5,
    });
  }
  return segs;
}
