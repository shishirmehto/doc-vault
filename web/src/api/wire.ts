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
/** The same body on 200 and on 503. */
export type HealthOut = S["HealthOut"];
export type ChatOut = S["ChatOut"];
export type ChatPage = S["ChatPage"];
export type TurnOut = S["TurnOut"];
export type HistoryOut = S["HistoryOut"];
export type CitationOut = S["CitationOut"];
export type StartChatIn = S["StartChatIn"];
export type AskIn = S["AskIn"];
export type DocumentMetricsOut = S["DocumentMetricsOut"];
export type ProcessingMetricsOut = S["ProcessingMetricsOut"];
export type PercentilesOut = S["PercentilesOut"];
export type StepMetricsOut = S["StepMetricsOut"];
