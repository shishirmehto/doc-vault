"use client";

import type { ReactNode } from "react";
import type { Selection } from "@/api/domain";
import { selectionQuery } from "@/api/parse";

/** A real link the app follows itself, without a reload. With a modifier key the browser opens it elsewhere. */
export function InAppLink({ to, onFollow, className, children }: {
  to: Selection;
  onFollow(): void;
  className?: string;
  children: ReactNode;
}) {
  return (
    <a
      className={className}
      href={selectionQuery(to)}
      onClick={(e) => {
        if (e.metaKey || e.ctrlKey || e.shiftKey) return;
        e.preventDefault();
        onFollow();
      }}
    >
      {children}
    </a>
  );
}
