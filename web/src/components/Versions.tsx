"use client";

import { useRef } from "react";
import type { Doc, DocumentId } from "@/api/domain";
import type { Versions } from "@/hooks/useVersions";
import type { Family } from "@/lib/versions";
import { newerVersion, otherVersions, versionLine, versionMeta, VERSIONS_FAILED } from "@/copy";
import { ACCEPT } from "./DropZone";
import { InAppLink } from "./InAppLink";

type Open = (id: DocumentId) => void;

function VersionLink({ doc, onOpen, children }: { doc: Doc; onOpen: Open; children: string }) {
  return <InAppLink to={{ kind: "doc", doc: doc.id, pane: null }} onFollow={() => onOpen(doc.id)}>{children}</InAppLink>;
}

/** "Version 2 of 2. Earlier version: version 1." The links come once the family is read. */
export function VersionLine({ doc, versions, family, onOpen }: { doc: Doc; versions: Versions; family: Family | null; onOpen: Open }) {
  const others = family?.others ?? [];
  return (
    <p className="doc-version">
      <span className="doc-version-number">{versionLine(doc)}.</span>{" "}
      {versions.kind === "failed" ? (
        <>
          {VERSIONS_FAILED}{" "}
          <button type="button" className="text-button" onClick={versions.retry}>Try again</button>
        </>
      ) : family && (
        <>
          {otherVersions(doc, others.length)}
          {others.map((d, i) => (
            <span key={d.id}>
              {" "}
              <VersionLink doc={d} onOpen={onOpen}>{`version ${d.version.number}`}</VersionLink>
              {i < others.length - 1 ? "," : "."}
            </span>
          ))}
        </>
      )}
    </p>
  );
}

/** On an earlier version: says a newer one exists, with a link to it once the family is read. */
export function NewerVersionNote({ doc, newest, onOpen }: { doc: Doc; newest: Doc | null; onOpen: Open }) {
  return (
    <p className="newer-version">
      {newerVersion(doc, newest)}{" "}
      {newest && <VersionLink doc={newest} onOpen={onOpen}>{`Open version ${newest.version.number}`}</VersionLink>}
    </p>
  );
}

/** Every version, oldest first, with what it was uploaded as, when, and where it stands. */
export function VersionList({ id, doc, family, onOpen }: { id: string; doc: Doc; family: Family; onOpen: Open }) {
  const all = [...family.others, doc].sort((a, b) => a.version.number - b.version.number);
  return (
    <ol className="version-list" id={id} aria-label="Versions of this document">
      {all.map((d) => (
        <li key={d.id} aria-current={d.id === doc.id ? "page" : undefined}>
          {d.id === doc.id
            ? <span className="version-list-name">Version {d.version.number}, this page</span>
            : <VersionLink doc={d} onOpen={onOpen}>{`Version ${d.version.number}`}</VersionLink>}
          <span className="row-meta">{versionMeta(d)}</span>
        </li>
      ))}
    </ol>
  );
}

/** "Upload a new version": one file, sent as the next version of this document whatever its name. */
export function UploadVersion({ onFiles }: { onFiles(files: File[]): void }) {
  const input = useRef<HTMLInputElement>(null);
  return (
    <>
      <button type="button" className="text-button" onClick={() => input.current?.click()}>Upload a new version</button>
      <input
        ref={input}
        type="file"
        accept={ACCEPT}
        hidden
        onChange={(e) => {
          const files = [...(e.currentTarget.files ?? [])];
          e.currentTarget.value = "";
          if (files.length > 0) onFiles(files);
        }}
      />
    </>
  );
}
