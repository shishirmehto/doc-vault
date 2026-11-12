"use client";

import { useEffect, useState } from "react";

const LONG_AFTER_MS = 5000;

export function useLongRun(startedAt: number): boolean {
  const [long, setLong] = useState(false);
  useEffect(() => {
    const timer = setTimeout(() => setLong(true), Math.max(0, startedAt + LONG_AFTER_MS - Date.now()));
    return () => clearTimeout(timer);
  }, [startedAt]);
  return long;
}
