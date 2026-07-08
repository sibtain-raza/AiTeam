import type { StationState } from "../lib/stations";

interface Props {
  name: string;
  badge: string;
  state: StationState;
  left: number;
  top: number;
  width: number;
  height: number;
}

const LED_TEXT: Record<StationState["lifecycle"], string> = {
  idle: "Idle",
  working: "Working",
  done: "Done",
  failed: "Failed",
};

export function Station({ name, badge, state, left, top, width, height }: Props) {
  return (
    <div className={`station ${state.lifecycle}`} style={{ left, top, width, height }}>
      <div className="station-head">
        <span className="badge">{badge}</span>
        <span className="station-name">{name}</span>
        <span className="led-row">
          <span className="led" />
          <span className="led-text">{LED_TEXT[state.lifecycle]}</span>
        </span>
      </div>
      <div className="station-status">
        {state.statusLine}
        {state.lifecycle === "working" && <span className="cursor" />}
      </div>
      {state.loopCount > 1 && <div className="loop-chip">REWORK {state.loopCount}</div>}
    </div>
  );
}
