import { Station } from "./Station";
import type { StationState } from "../lib/stations";

/** Fixed layout mirroring pipeline.py's actual graph shape (see
 * SPEC.md §1 / README "The pipeline"): PM -> Architect -> {FE,BE,OPS} ->
 * QA -> UAT -> Reporter down a center spine, QA_FAIL rework routed as a
 * dashed channel on the left, UAT_REJECTED re-scope on the right. Ported
 * verbatim from the approved static mockup. */
const LAYOUT = {
  pm: { left: 550, top: 30, width: 220, height: 100 },
  architect: { left: 550, top: 175, width: 220, height: 100 },
  fe: { left: 170, top: 320, width: 200, height: 100 },
  be: { left: 560, top: 320, width: 200, height: 100 },
  ops: { left: 950, top: 320, width: 200, height: 100 },
  qa: { left: 550, top: 470, width: 220, height: 130 },
  uat: { left: 550, top: 650, width: 220, height: 100 },
  reporter: { left: 550, top: 800, width: 220, height: 100 },
};

interface Props {
  stations: Record<string, StationState>;
}

export function Schematic({ stations }: Props) {
  const idle: StationState = { lifecycle: "idle", statusLine: "waiting", loopCount: 0 };

  return (
    <div className="schematic-frame">
      <div className="canvas">
        <svg viewBox="0 0 1320 940" width={1320} height={940} aria-hidden="true">
          <path d="M660,130 L660,175" stroke="var(--teal-dim)" strokeWidth={2} fill="none" />
          <path d="M660,275 L660,300 L270,300 L270,320" stroke="var(--teal-dim)" strokeWidth={2} fill="none" />
          <path d="M660,275 L660,320" stroke="var(--teal-dim)" strokeWidth={2} fill="none" />
          <path d="M660,275 L660,300 L1050,300 L1050,320" stroke="var(--teal-dim)" strokeWidth={2} fill="none" />
          <path d="M270,420 L270,450 L660,450 L660,470" stroke="var(--teal-dim)" strokeWidth={2} fill="none" />
          <path d="M660,420 L660,470" stroke="var(--brass)" strokeWidth={2} fill="none" />
          <path d="M1050,420 L1050,450 L660,450 L660,470" stroke="var(--teal-dim)" strokeWidth={2} fill="none" />
          <path d="M660,600 L660,650" stroke="var(--idle)" strokeWidth={2} fill="none" strokeDasharray="1 6" strokeLinecap="round" />
          <text className="edge-label" x={674} y={629}>QA_PASS</text>
          <path d="M660,750 L660,800" stroke="var(--idle)" strokeWidth={2} fill="none" strokeDasharray="1 6" strokeLinecap="round" />
          <text className="edge-label" x={674} y={779}>UAT_APPROVED</text>

          <path
            d="M550,535 L60,535 L60,305 L270,305 L270,320 M660,305 L660,320 M1050,305 L1050,320 M60,305 L1050,305"
            stroke="var(--brass)" strokeWidth={1.6} fill="none" strokeDasharray="6 5" opacity={0.85}
          />
          <text className="edge-label brass" x={70} y={500} transform="rotate(-90 70 500)">QA_FAIL → rework</text>

          <path d="M770,700 L1260,700 L1260,80 L770,80" stroke="var(--brick)" strokeWidth={1.6} fill="none" strokeDasharray="6 5" opacity={0.85} />
          <text className="edge-label brick" x={1233} y={400} transform="rotate(-90 1233 400)">UAT_REJECTED → re-scope</text>
        </svg>

        <Station name="product_manager" badge="PM" state={stations.product_manager ?? idle} {...LAYOUT.pm} />
        <Station name="solution_architect" badge="AR" state={stations.solution_architect ?? idle} {...LAYOUT.architect} />
        <Station name="frontend_engineer" badge="FE" state={stations.frontend_engineer ?? idle} {...LAYOUT.fe} />
        <Station name="backend_engineer" badge="BE" state={stations.backend_engineer ?? idle} {...LAYOUT.be} />
        <Station name="devops_engineer" badge="OP" state={stations.devops_engineer ?? idle} {...LAYOUT.ops} />
        <Station name="qa_engineer" badge="QA" state={stations.qa_engineer ?? idle} {...LAYOUT.qa} />
        <Station name="uat_reviewer" badge="UT" state={stations.uat_reviewer ?? idle} {...LAYOUT.uat} />
        <Station name="release_reporter" badge="RR" state={stations.release_reporter ?? idle} {...LAYOUT.reporter} />
      </div>
    </div>
  );
}
