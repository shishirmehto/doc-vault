"use client";

import { Suspense } from "react";
import Link from "next/link";
import { usePathname, useSearchParams } from "next/navigation";
import { parseSelection } from "@/api/parse";
import { useHealth } from "@/hooks/useHealth";
import { useLiveStatus } from "@/hooks/useLiveFeed";

/** One note at a time: a service that is down, then live updates that are paused, then the fake provider. */
function ServiceNote() {
  const health = useHealth();
  const live = useLiveStatus();
  if (health?.ok === false) return <span className="service" data-tone="failed">Vault service unreachable</span>;
  if (health?.ok && health.value.unreachable.length > 0) {
    return <span className="service" data-tone="failed">Unreachable: {health.value.unreachable.join(", ")}</span>;
  }
  if (live === "paused") return <span className="service" role="status">Live updates paused, retrying</span>;
  if (health?.ok && health.value.aiMode === "fake") {
    return (
      <>
        <button type="button" className="service" popoverTarget="ai-note">Fake AI provider</button>
        <p id="ai-note" className="service-note" popover="auto">
          No API key is set, so summaries, key facts and answers come from the backend&apos;s deterministic fake model.
          They show the wiring and the citations, not answer quality.
        </p>
      </>
    );
  }
  return null;
}

/**
 * "page" when the link leads to exactly this screen, "true" when this screen sits inside the section, as a
 * chat sits inside Ask.
 */
type Current = { ask: "page" | "true" | undefined; metrics: "page" | undefined };

function Sections({ current }: { current: Current }) {
  return (
    <nav className="sections" aria-label="Sections">
      <Link href="/" aria-current={current.ask}>Ask</Link>
      <Link href="/?view=metrics" aria-current={current.metrics}>Metrics</Link>
    </nav>
  );
}

const NONE: Current = { ask: undefined, metrics: undefined };

function CurrentSections() {
  const path = usePathname();
  const params = useSearchParams();
  if (path !== "/") return <Sections current={NONE} />;
  const selection = parseSelection(new URLSearchParams(params.toString()));
  switch (selection.kind) {
    case "none":
      return <Sections current={{ ask: "page", metrics: undefined }} />;
    case "chat":
      return <Sections current={{ ask: "true", metrics: undefined }} />;
    case "metrics":
      return <Sections current={{ ask: undefined, metrics: "page" }} />;
    case "doc":
    case "missing":
      return <Sections current={NONE} />;
    default: {
      const unknown: never = selection;
      return unknown;
    }
  }
}

export function TopBar() {
  return (
    <header className="topbar">
      <Link className="wordmark" href="/">Doc Vault</Link>
      <Suspense fallback={<Sections current={NONE} />}>
        <CurrentSections />
      </Suspense>
      <ServiceNote />
    </header>
  );
}
