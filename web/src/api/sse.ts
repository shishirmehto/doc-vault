// Frames a text/event-stream body into messages, by the WHATWG server-sent events parsing rules.

export type SseMessage = { event: string; data: string };

export type SseReader = {
  push(text: string): SseMessage[];
};

const LINE_END = /\r\n|\r|\n/;

export function createSseReader(): SseReader {
  let partial = "";
  let skipLf = false;
  let event = "";
  let data: string[] = [];

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
