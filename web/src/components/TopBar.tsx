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
 * The section this screen sits in. `at` is "page" when the section's link leads to exactly this screen, and "true"
 * when the screen sits inside the section, as a chat sits inside Ask.
 */
type Current = { section: "ask" | "compare" | "metrics"; at: "page" | "true" } | null;

function Sections({ current }: { current: Current }) {
  const at = (section: NonNullable<Current>["section"]) => (current?.section === section ? current.at : undefined);
  return (
    <nav className="sections" aria-label="Sections">
      <Link href="/" aria-current={at("ask")}>Ask</Link>
      <Link href="/?view=compare" aria-current={at("compare")}>Compare</Link>
      <Link href="/?view=metrics" aria-current={at("metrics")}>Metrics</Link>
    </nav>
  );
}

function CurrentSections() {
  const path = usePathname();
  const params = useSearchParams();
  if (path !== "/") return <Sections current={null} />;
  const selection = parseSelection(new URLSearchParams(params.toString()));
  switch (selection.kind) {
    case "none":
      return <Sections current={{ section: "ask", at: "page" }} />;
    case "chat":
      return <Sections current={{ section: "ask", at: "true" }} />;
    case "compare":
      return <Sections current={{ section: "compare", at: "page" }} />;
    case "comparison":
      return <Sections current={{ section: "compare", at: "true" }} />;
    case "metrics":
      return <Sections current={{ section: "metrics", at: "page" }} />;
    case "doc":
    case "missing":
      return <Sections current={null} />;
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
      <Suspense fallback={<Sections current={null} />}>
        <CurrentSections />
      </Suspense>
      <ServiceNote />
    </header>
  );
}
