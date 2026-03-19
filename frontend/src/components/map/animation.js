export function interpolateBearing(start, end, progress) {
  const a = start ?? 0;
  const b = end ?? a;
  let delta = ((b - a + 540) % 360) - 180;
  return (a + delta * progress + 360) % 360;
}

export function easeInOutCubic(progress) {
  return progress < 0.5
    ? 4 * progress * progress * progress
    : 1 - Math.pow(-2 * progress + 2, 3) / 2;
}
