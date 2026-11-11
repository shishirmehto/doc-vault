const activity = (v: any): string | null => {
  const pcs = Object.values(v.pc) as string[];
  const settled = v.status === "ready" || v.status === "failed";
  const working = settled ? ["parsing", "embedding"] : ["checking", "parsing", "embedding"];
  if (pcs.some((p) => working.includes(p))) return "a worker holds the lease";
  if (pcs.includes("failing")) return "the worker marks it failed (lease held)";
  return null;
};

const statusLabel: Record<string, string> = {
  queued: "Queued",
  parsing: "Parsing",
  embedding: "Embedding",
  ready: "Ready for chat",
  failed: "Failed",
};

export const title = "doc-vault: one document through indexing";

export const state = (v: any) => {
  const top = statusLabel[v.status];
  const doing = activity(v);
  if (v.status === "ready" && !doing) return [top, "nothing running"];
  if (v.status === "failed" && !doing) return [top, "waiting for you to retry"];
  return [top, doing ?? "waiting for a worker"];
};

export const group = (v: any) =>
  v.status === "ready" ? "done" : v.status === "failed" ? "failed" : "working";

export const colors = { working: "blue", done: "teal", failed: "orange" };

export const step = (action: string, from: any, to: any): string | null => {
  switch (action) {
    case "Take":
    case "GetLease":
      return "a worker picks up the job";
    case "LeaseBusy":
      return "a duplicate job finds the lease taken and exits";
    case "NothingToDo":
      return "the worker finds nothing left to do";
    case "StartParse":
      return "the worker starts parsing";
    case "ResumeEmbed":
      return "the worker resumes embedding (pages already saved)";
    case "ParseSaved":
      return "pages and chunks are saved";
    case "BadFile":
      return "the file has no readable text";
    case "ProviderFault":
      return to.pc && Object.values(to.pc).includes("failing")
        ? "the AI provider keeps failing (retries used up)"
        : "the AI provider fails once (retry queued)";
    case "EmbedBatch":
      return "a batch of chunks is embedded";
    case "EmbedDone":
      return "the last chunk is embedded";
    case "MarkFailed":
      return "the job marks the document failed";
    case "Unlock":
      return "the worker releases the lease";
    case "Crash":
      return "the worker crashes (job redelivered)";
    case "Reconcile":
      return "the minute check re-queues a stale document";
    case "UserRetry":
      return "you press retry";
    case "Settled":
      return null;
    default:
      return action;
  }
};
