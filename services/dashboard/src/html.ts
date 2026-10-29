/**
 * The one HTML escaping module of the dashboard. Everything that reaches
 * `innerHTML` and comes from the wire (model output, flag keys, game ids, ...)
 * goes through `escapeHtml` or the `html` template tag.
 */

/** Escape text for HTML element content AND quoted attribute values (both quote kinds). */
export function escapeHtml(text: string): string {
  return text
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

/** Markup that is already safe (built with `html` or by a trusted literal): `html` does not escape it again. */
export class SafeHtml {
  constructor(readonly value: string) {}
  toString(): string {
    return this.value;
  }
}

/** Mark a trusted literal as markup. Never pass wire data. */
export function raw(markup: string): SafeHtml {
  return new SafeHtml(markup);
}

/**
 * Tagged template: every interpolation is escaped (text and quoted attribute
 * values alike) unless it is a `SafeHtml`; arrays are joined, null / undefined / false render empty.
 */
export function html(strings: TemplateStringsArray, ...values: unknown[]): SafeHtml {
  let out = strings[0];
  values.forEach((value, i) => {
    out += render(value) + strings[i + 1];
  });
  return new SafeHtml(out);
}

function render(value: unknown): string {
  if (value instanceof SafeHtml) return value.value;
  if (Array.isArray(value)) return value.map(render).join("");
  if (value === null || value === undefined || value === false) return "";
  return escapeHtml(String(value));
}
