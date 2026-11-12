"use client";

import { useEffect, useRef } from "react";
import { focusDropped } from "@/lib/focus";

export function useCatchDroppedFocus<T extends HTMLElement>(navigated: boolean) {
  const target = useRef<T>(null);
  useEffect(() => {
    if (navigated && focusDropped(document)) target.current?.focus({ preventScroll: true });
  }, [navigated]);
  return target;
}
