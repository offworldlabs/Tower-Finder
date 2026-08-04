/**
 * Emergency-squawk audio alert. Uses the Web Audio API directly — no asset
 * files needed. A short two-tone beep plays once per (hex, squawk) so the
 * same aircraft doesn't re-alert on every frame.
 *
 * Tower-Finder watches: 7500 (hijack), 7600 (radio fail), 7700 (general
 * emergency).
 */

const EMERGENCY_SQUAWKS = new Set(["7500", "7600", "7700"]);
const _alertedKeys = new Set<string>();
let _audioCtx: AudioContext | null = null;

function getCtx(): AudioContext | null {
  if (typeof window === "undefined") return null;
  if (!_audioCtx) {
    const AC = (window as any).AudioContext || (window as any).webkitAudioContext;
    if (!AC) return null;
    try {
      _audioCtx = new AC();
    } catch {
      return null;
    }
  }
  return _audioCtx;
}

function beep(freq: number, durationMs: number, startOffset = 0) {
  const ctx = getCtx();
  if (!ctx) return;
  const now = ctx.currentTime + startOffset;
  const osc = ctx.createOscillator();
  const gain = ctx.createGain();
  osc.frequency.value = freq;
  osc.type = "sine";
  // Quick attack/decay envelope to avoid clicks
  gain.gain.setValueAtTime(0, now);
  gain.gain.linearRampToValueAtTime(0.18, now + 0.01);
  gain.gain.exponentialRampToValueAtTime(0.0001, now + durationMs / 1000);
  osc.connect(gain).connect(ctx.destination);
  osc.start(now);
  osc.stop(now + durationMs / 1000 + 0.02);
}

/** Play the canonical two-tone emergency chirp. */
export function playEmergencyChime() {
  beep(880, 220, 0);
  beep(660, 220, 0.25);
}

/**
 * Drive the alerter from the latest aircraft list. Returns the set of
 * (hex, squawk) keys that triggered this call so the caller can show a
 * banner if desired.
 */
export function checkEmergencySquawks(
  aircraft: Array<{ hex: string; squawk?: string | number | null }>,
  enabled: boolean,
): Array<{ hex: string; squawk: string }> {
  const triggered: Array<{ hex: string; squawk: string }> = [];
  for (const ac of aircraft) {
    if (!ac?.hex || ac.squawk == null) continue;
    const sq = String(ac.squawk);
    if (!EMERGENCY_SQUAWKS.has(sq)) continue;
    const key = `${ac.hex}::${sq}`;
    if (_alertedKeys.has(key)) continue;
    // Record the key even while muted — the early `if (!enabled) return`
    // used to skip this, so toggling sound back on re-alerted (and
    // re-toasted) every squawk already on screen.
    _alertedKeys.add(key);
    if (enabled) triggered.push({ hex: ac.hex, squawk: sq });
  }
  if (triggered.length > 0) {
    try {
      playEmergencyChime();
    } catch {
      /* ignore audio failures (autoplay policy etc.) */
    }
  }
  return triggered;
}

/** Forget all previously-alerted keys (e.g. when user resumes a paused feed). */
export function resetEmergencyAlertCache() {
  _alertedKeys.clear();
}
