// Names for the wire shapes generated from the API's /openapi.json (npm run gen:api).
// Only parse.ts and client.ts import this file, so no wire shape reaches a component.
import type { components } from "./schema";

type S = components["schemas"];

export type DocumentOut = S["DocumentOut"];
export type DocumentPage = S["DocumentPage"];
export type PageOut = S["PageOut"];
export type ErrorBody = S["ErrorBody"];
export type InsightsOut =
  | S["InsightsReadyOut"]
  | S["InsightsPendingOut"]
  | S["InsightsFailedOut"]
  | S["InsightsUnavailableOut"];
// GET /health declares no response model: {db, redis, ai_mode, embed_model}, on 200 and on 503.
export type HealthOut = Record<string, string>;
