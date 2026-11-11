import type { ReactNode } from "react";
import Link from "next/link";

/** Something the address names is not there: say what, and offer the way back. */
export function Missing({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="missing" aria-labelledby="missing-title">
      <h1 id="missing-title">{title}</h1>
      <p className="lead">{children}</p>
      <p>
        <Link className="button" href="/">Ask your documents</Link>
      </p>
    </section>
  );
}

export function MissingDocument() {
  return <Missing title="This document isn't in the vault">It may have been deleted, or the link may be wrong.</Missing>;
}

export function MissingChat() {
  return <Missing title="This chat isn't in the vault">The link may be wrong or out of date.</Missing>;
}
