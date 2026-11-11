"use client";

import { useEffect, useRef, useState } from "react";
import { api } from "@/api/client";
import type { DocumentId, Result, Summary, SummaryCite, SummaryId, SummaryLength, SummaryList, SummaryOptions, SummaryTone } from "@/api/domain";
import { useFetched } from "@/hooks/useFetched";
import { LISTED, idle, mergeSummary, shownSummary, step, type Run, type RunEvent } from "@/lib/summary";

/** The options as typed; the focus is normalised when it is sent. */
export type Draft = { length: SummaryLength; focus: string; tone: SummaryTone };

export type Rewrites = ReturnType<typeof useRewrites>;

/**
 * One document's rewritten summaries, the request in flight, and the disclosure's state. The document screen holds
 * it, because the disclosure moves within the insights panel as the insights load, which remounts it, and the source
 * pane resolves a summary's cite against the same list. `closeCite` takes the cite out of the URL.
 */
export function useRewrites(id: DocumentId, cite: SummaryCite | null, closeCite: () => void) {
  const listed = useFetched(`summaries:${id}`, () => api.summaries(id, LISTED));
  // This session's results, newest first: the list read on opening does not hold them.
  const [made, setMade] = useState<readonly Summary[]>([]);
  // The result the live region announces, until the next run starts.
  const [landed, setLanded] = useState<Summary | null>(null);
  const [chosen, setChosen] = useState<SummaryId | null>(cite?.summary ?? null);
  const [open, setOpen] = useState(cite !== null);
  const [draft, setDraft] = useState<Draft>({ length: "standard", focus: "", tone: "plain" });

  // A cite that arrives (a link, Back) opens the disclosure on its summary, which stays shown after Close.
  const cited = cite?.summary ?? null;
  const [seen, setSeen] = useState(cited);
  if (seen !== cited) {
    setSeen(cited);
    if (cited !== null) {
      setChosen(cited);
      setOpen(true);
    }
  }

  // The shown summary always owns the open cite, so choosing another summary, or a run landing, closes it.
  useEffect(() => {
    if (cite !== null && chosen !== cite.summary) closeCite();
  }, [cite, chosen, closeCite]);

  // The ref decides, so a second start in the same tick cannot send a second request.
  const runRef = useRef<Run>(idle);
  const [run, setRun] = useState<Run>(idle);
  const send = (event: RunEvent): boolean => {
    const next = step(runRef.current, event);
    if (next === runRef.current) return false;
    runRef.current = next;
    setRun(next);
    return true;
  };

  const start = async (options: SummaryOptions) => {
    if (!send({ type: "start", options, at: Date.now() })) return;
    setLanded(null);
    const result = await api.summarize(id, options);
    if (result.ok) {
      setMade((list) => mergeSummary(list, result.value));
      setChosen(result.value.id);
      setLanded(result.value);
    }
    send(result.ok ? { type: "succeeded" } : { type: "failed", error: result.error });
  };

  const list: Result<SummaryList> | null = listed.result?.ok
    ? { ok: true, value: { ...listed.result.value, summaries: made.reduceRight(mergeSummary, listed.result.value.summaries) } }
    : listed.result;

  return {
    list,
    reload: listed.reload,
    run,
    start,
    landed,
    shown: shownSummary(list?.ok ? list.value.summaries : made, chosen, cite),
    choose: setChosen,
    open,
    setOpen,
    draft,
    /** A changed option makes a failure's Try again, which sends the old options, misleading, so the failure goes. */
    edit: (change: Partial<Draft>) => {
      setDraft((d) => ({ ...d, ...change }));
      send({ type: "reset" });
    },
  };
}
