export function focusDropped(page: { activeElement: object | null; body: object | null }): boolean {
  return page.activeElement === null || page.activeElement === page.body;
}
