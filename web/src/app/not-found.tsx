import { Missing } from "@/components/Missing";

export default function NotFound() {
  return (
    <main className="centre">
      <Missing title="There is no page at this address">
        Doc Vault has one screen for asking and one for metrics. The address may be mistyped, or the link may be out of
        date.
      </Missing>
    </main>
  );
}
