import { Suspense } from "react";
import { Workspace } from "@/components/Workspace";

// One route. The URL's search params (?doc, ?cite, ?p) hold the selection, so Back closes the
// source pane and a pasted link reopens it. Everything is fetched in the browser through /api.
export default function Home() {
  return (
    <Suspense fallback={<div className="workspace" />}>
      <Workspace />
    </Suspense>
  );
}
