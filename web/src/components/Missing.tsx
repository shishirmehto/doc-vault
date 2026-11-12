import type { ReactNode } from "react";
import Link from "next/link";

type Way = { href: string; label: string };

const ASK: Way = { href: "/", label: "Ask your documents" };

export function Missing({ title, way = ASK, children }: { title: string; way?: Way; children: ReactNode }) {
  return (
    <section className="missing" aria-labelledby="missing-title">
      <h1 id="missing-title">{title}</h1>
      <p className="lead">{children}</p>
      <p>
        <Link className="button" href={way.href}>{way.label}</Link>
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

export function MissingComparison() {
  return (
    <Missing title="This comparison isn't in the vault" way={{ href: "/?view=compare", label: "Compare documents" }}>
      The link may be wrong or out of date.
    </Missing>
  );
}
