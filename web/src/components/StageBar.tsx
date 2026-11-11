import type { Lifecycle } from "@/api/domain";
import { stageProgress, type Segment } from "@/lib/lifecycle";

const NAMES = ["Parse", "Embed", "Ready"] as const;

function fill(s: Segment): { width: string; state: string } {
  switch (s.kind) {
    case "todo":
      return { width: "0%", state: "todo" };
    case "active":
      return s.fraction === null ? { width: "35%", state: "moving" } : { width: `${Math.round(s.fraction * 100)}%`, state: "active" };
    case "done":
      return { width: "100%", state: "done" };
    case "failed":
      return { width: "100%", state: "failed" };
  }
}

/** Three segments, parse, embed and ready. The status sentence next to it carries the meaning. */
export function StageBar({ lifecycle }: { lifecycle: Lifecycle }) {
  return (
    <div className="stagebar" aria-hidden="true">
      {stageProgress(lifecycle).map((segment, i) => {
        const { width, state } = fill(segment);
        return (
          <span key={NAMES[i]} className="stage" data-state={state} title={NAMES[i]}>
            <span style={{ width }} />
          </span>
        );
      })}
    </div>
  );
}
