/** "v2" beside a document's name or meta. Screen readers hear "Version 2". Nothing for null. */
export function VersionMark({ number }: { number: number | null }) {
  if (number === null) return null;
  return (
    <span className="version-mark">
      <span aria-hidden="true">v{number}</span>
      <span className="visually-hidden">Version {number}.</span>
    </span>
  );
}
