export default function PlaybackBar({ history, onSeek, formatSecondsAgo }) {
  if (!history.length) return null;

  return (
    <div className="playback-bar">
      <span className="playback-time">{formatSecondsAgo(history[0].ts)}</span>
      <input
        type="range"
        min={0}
        max={history.length - 1}
        defaultValue={history.length - 1}
        onChange={(e) => onSeek(Number(e.target.value))}
      />
      <span className="playback-time">{formatSecondsAgo(history[history.length - 1].ts)}</span>
    </div>
  );
}
