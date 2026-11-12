"use client";

import { useEffect, useRef, useState } from "react";
import { api } from "@/api/client";
import type { DocumentId, Result, Summary, SummaryCite, SummaryId, SummaryLength, SummaryList, SummaryOptions, SummaryTone } from "@/api/domain";
import { useFetched } from "@/hooks/useFetched";
import { LISTED, idle, mergeSummary, paneList, shownSummary, step, type Run, type RunEvent } from "@/lib/summary";

type Draft = { length: SummaryLength; focus: string; tone: SummaryTone };

export type Rewrites = ReturnType<typeof useRewrites>;

export function useRewrites(id: DocumentId, cite: SummaryCite | null, closeCite: () => void) {
  const listed = useFetched(`summaries:${id}`, () => api.summaries(id, LISTED));
  const [made, setMade] = useState<readonly Summary[]>([]);
  const [landed, setLanded] = useState<Summary | null>(null);
  const [chosen, setChosen] = useState<SummaryId | null>(cite?.summary ?? null);
  const [open, setOpen] = useState(cite !== null);
  const [draft, setDraft] = useState<Draft>({ length: "standard", focus: "", tone: "plain" });

  const cited = cite?.summary ?? null;
  const [seen, setSeen] = useState(cited);
  if (seen !== cited) {
    setSeen(cited);
    if (cited !== null) {
      setChosen(cited);
      setOpen(true);
    }
  }

  useEffect(() => {
    if (cite !== null && chosen !== cite.summary) closeCite();
  }, [cite, chosen, closeCite]);

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
    paneList: paneList(list, made, cite),
    reload: listed.reload,
    run,
    start,
    landed,
    shown: shownSummary(list?.ok ? list.value.summaries : made, chosen, cite),
    choose: setChosen,
    open,
    setOpen,
    draft,
    edit: (change: Partial<Draft>) => {
      setDraft((d) => ({ ...d, ...change }));
      send({ type: "reset" });
    },
  };
}
