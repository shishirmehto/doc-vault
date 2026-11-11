"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { api } from "@/api/client";
import type { Health, Result } from "@/api/domain";

const HEALTH_MS = 30_000;

function ServiceNote() {
  const [health, setHealth] = useState<Result<Health> | null>(null);
  useEffect(() => {
    let alive = true;
    const check = () => void api.health().then((r) => alive && setHealth(r));
    check();
    const timer = setInterval(check, HEALTH_MS);
    return () => {
      alive = false;
      clearInterval(timer);
    };
  }, []);

  if (health === null) return null;
  if (!health.ok) return <span className="service" data-tone="failed">Vault service unreachable</span>;
  if (health.value.unreachable.length > 0) {
    return <span className="service" data-tone="failed">Unreachable: {health.value.unreachable.join(", ")}</span>;
  }
  if (health.value.aiMode === "fake") {
    return (
      <span className="service" title="No API key is set. Summaries and key facts come from the backend's deterministic fake model.">
        Fake AI provider
      </span>
    );
  }
  return null;
}

export function TopBar() {
  return (
    <header className="topbar">
      <Link className="wordmark" href="/">Doc Vault</Link>
      <ServiceNote />
    </header>
  );
}
