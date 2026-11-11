// Frames a text/event-stream body into messages, by the WHATWG server-sent events parsing rules.

export type SseMessage = { event: string; data: string };

export type SseReader = {
  /**
   * Takes decoded text split anywhere. Returns the messages it completes, in order. A message whose blank
   * line never arrives is never returned, as the spec discards it when the body ends.
   */
  push(text: string): SseMessage[];
};

const LINE_END = /\r\n|\r|\n/;

export function createSseReader(): SseReader {
  let partial = "";
  let skipLf = false;
  let event = "";
  let data: string[] = [];

  // A comment line (leading colon) parses as the field "", which matches nothing.
  function take(line: string): SseMessage[] {
    if (line === "") {
      const done = data.length === 0 ? [] : [{ event: event || "message", data: data.join("\n") }];
      event = "";
      data = [];
      return done;
    }
    const colon = line.indexOf(":");
    const field = colon === -1 ? line : line.slice(0, colon);
    const value = colon === -1 ? "" : line.slice(colon + 1).replace(/^ /, "");
    if (field === "event") event = value;
    if (field === "data") data.push(value);
    return [];
  }

  return {
    push(text) {
      // A decoder returns "" for a chunk that ends inside a character; a pending CR must survive it.
      if (text === "") return [];
      const body = skipLf && text.startsWith("\n") ? text.slice(1) : text;
      skipLf = text.endsWith("\r");
      const [head = "", ...rest] = body.split(LINE_END);
      const lines = [partial + head, ...rest];
      partial = lines.pop() ?? "";
      return lines.flatMap(take);
    },
  };
}
