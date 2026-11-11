// Run in the page: Playwright `page.evaluate(source)`, or paste into a browser tool that evaluates an expression.
// Returns one finding per problem, and [] when everything on screen can be read and reached:
//   clipped      text cut off by overflow: hidden or clip, text-overflow: ellipsis, or line-clamp
//   unreachable  a button, link or field outside the viewport that no scroll container can bring into view
//   page-scroll  the page itself is wider than the viewport
//   side-scroll  a column scrolls sideways, unless it is marked data-scroll-x (a wide table's own box)
// features/README.md lists what it skips and why.
(() => {
  const TOLERANCE = 1;
  const findings = [];
  const root = document.documentElement;
  const vw = root.clientWidth;
  const vh = root.clientHeight;

  const style = (el) => getComputedStyle(el);
  const scrolls = (v) => v === "auto" || v === "scroll";
  const clips = (v) => v === "hidden" || v === "clip";
  const rendered = (el) => el.getClientRects().length > 0 && style(el).visibility !== "hidden";
  const round = (r) => ({ top: Math.round(r.top), left: Math.round(r.left), bottom: Math.round(r.bottom), right: Math.round(r.right) });
  const name = (el) => {
    const id = el.id ? `#${el.id}` : "";
    const classes = typeof el.className === "string" && el.className.trim() ? `.${el.className.trim().split(/\s+/).join(".")}` : "";
    return `${el.tagName.toLowerCase()}${id}${classes}`;
  };
  const text = (el) =>
    (el.getAttribute("aria-label") || el.innerText || el.value || el.getAttribute("placeholder") || "").replace(/\s+/g, " ").trim().slice(0, 80);

  // The root's overflow applies to the viewport. When the root is visible, body's overflow is the viewport's instead.
  const rootStyle = style(root);
  const viewportStyle = rootStyle.overflowX === "visible" && rootStyle.overflowY === "visible" ? style(document.body) : rootStyle;
  const pageScrolls = { x: !clips(viewportStyle.overflowX), y: !clips(viewportStyle.overflowY) };

  for (const el of [root, document.body, ...document.body.querySelectorAll("*")]) {
    if (el.matches("input, textarea, select, script, style") || !rendered(el)) continue;
    const s = style(el);
    if (el !== root && el !== document.body && (scrolls(s.overflowX) || scrolls(s.overflowY))) continue;
    if (!(el.innerText || "").trim()) continue;
    const reasons = [];
    const wide = el.scrollWidth > el.clientWidth + TOLERANCE;
    const tall = el.scrollHeight > el.clientHeight + TOLERANCE;
    const clamp = [s.getPropertyValue("-webkit-line-clamp"), s.getPropertyValue("line-clamp")].some((v) => v && v !== "none");
    const isViewport = el === root || (el === document.body && viewportStyle !== rootStyle);
    if (isViewport) {
      if (tall && !pageScrolls.y) reasons.push(`page content ${el.scrollHeight}px tall in a ${el.clientHeight}px viewport that does not scroll`);
    } else {
      const ellipsis = s.textOverflow === "ellipsis" ? ", ellipsis" : "";
      if (wide && clips(s.overflowX)) reasons.push(`overflow-x ${s.overflowX}${ellipsis}: ${el.scrollWidth}px in ${el.clientWidth}px`);
      if (tall && (clips(s.overflowY) || clamp)) reasons.push(`${clamp ? "line-clamp" : `overflow-y ${s.overflowY}`}: ${el.scrollHeight}px in ${el.clientHeight}px`);
    }
    if (reasons.length > 0) findings.push({ kind: "clipped", element: name(el), text: text(el), reasons });
  }

  // Walks out from the control. A clipping ancestor that is not a scroll container hides whatever falls outside
  // it for good. A scroll container can bring its content into view, so from there on the question is whether
  // the container itself can be seen. Last comes the viewport, which scrolls only if the page does.
  const reachable = (el) => {
    let box = el.getBoundingClientRect();
    let fixed = style(el).position === "fixed";
    for (let a = el.parentElement; a && a !== root && !fixed; a = a.parentElement) {
      const s = style(a);
      if (a === document.body && viewportStyle !== rootStyle) continue;
      const r = a.getBoundingClientRect();
      const outX = box.left < r.left - TOLERANCE || box.right > r.right + TOLERANCE;
      const outY = box.top < r.top - TOLERANCE || box.bottom > r.bottom + TOLERANCE;
      if (scrolls(s.overflowX) || scrolls(s.overflowY)) {
        if ((outX && !scrolls(s.overflowX)) || (outY && !scrolls(s.overflowY))) return false;
        // A scroller smaller than the control can never show all of it.
        if (a.clientWidth + TOLERANCE < box.width || a.clientHeight + TOLERANCE < box.height) return false;
        // The scroller can bring the control anywhere into its own box, so from here on the question is whether a
        // control-sized box inside it can be seen. Taking the whole scroller would call a sideways-scrolling table
        // taller than its column unreachable, though the column scrolls each row of it into view.
        const left = Math.max(r.left, Math.min(box.left, r.right - box.width));
        const top = Math.max(r.top, Math.min(box.top, r.bottom - box.height));
        box = { left, top, right: left + box.width, bottom: top + box.height, width: box.width, height: box.height };
      } else if ((outX && clips(s.overflowX)) || (outY && clips(s.overflowY))) {
        return false;
      }
      fixed = s.position === "fixed";
    }
    const outX = box.left < -TOLERANCE || box.right > vw + TOLERANCE;
    const outY = box.top < -TOLERANCE || box.bottom > vh + TOLERANCE;
    return !((outX && !pageScrolls.x) || (outY && !pageScrolls.y));
  };

  const controls = document.body.querySelectorAll("a[href], button, input:not([type='hidden']), select, textarea, summary");
  for (const el of controls) {
    if (!rendered(el) || el.closest("[inert]")) continue;
    if (!reachable(el)) findings.push({ kind: "unreachable", element: name(el), text: text(el), box: round(el.getBoundingClientRect()), viewport: { width: vw, height: vh } });
  }

  const pageWidth = Math.max(root.scrollWidth, document.body.scrollWidth);
  if (pageWidth > vw + TOLERANCE) findings.push({ kind: "page-scroll", element: "html", text: `${pageWidth}px wide in a ${vw}px viewport` });

  // The page itself never scrolls here, so content too wide for its column lands in the column's scroller.
  for (const el of document.body.querySelectorAll("*")) {
    if (el.matches("input, textarea, select") || el.hasAttribute("data-scroll-x") || !rendered(el)) continue;
    if (scrolls(style(el).overflowX) && el.scrollWidth > el.clientWidth + TOLERANCE) {
      findings.push({ kind: "side-scroll", element: name(el), text: `${el.scrollWidth}px wide in ${el.clientWidth}px` });
    }
  }

  return findings;
})();
