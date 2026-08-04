export default function PlaybackBar({ history, value, onSeek, formatSecondsAgo }) {
  if (!history.length) return null;

  // Controlled: an uncontrolled defaultValue desynced the thumb from the
  // frame actually displayed as history grew while paused.
  return (
    <div className="playback-bar">
      <span className="playback-time">{formatSecondsAgo(history[0].ts)}</span>
      <input
        type="range"
        min={0}
        max={history.length - 1}
        value={Math.min(value ?? history.length - 1, history.length - 1)}
        onChange={(e) => onSeek(Number(e.target.value))}
      />
      <span className="playback-time">{formatSecondsAgo(history[history.length - 1].ts)}</span>
    </div>
  );
}
