/**
 * Whether a screen's heading takes keyboard focus as it appears. Only after the reader moved to the screen within the
 * app, and only when focus fell to the page body because the control that held it left with the previous screen. A
 * first load keeps the browser's start, and a control the reader moved to keeps its focus.
 */
export function headingTakesFocus(arrived: boolean, page: { activeElement: object | null; body: object | null }): boolean {
  return arrived && (page.activeElement === null || page.activeElement === page.body);
}
