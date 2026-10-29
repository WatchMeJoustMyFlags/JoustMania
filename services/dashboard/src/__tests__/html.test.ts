import { describe, expect, it } from "vitest";
import { escapeHtml, html, raw } from "../html.js";

describe("UC-035: html escaping module", () => {
  it("escapes text-context metacharacters", () => {
    expect(escapeHtml(`<script>alert(1)</script> & co`)).toBe("&lt;script&gt;alert(1)&lt;/script&gt; &amp; co");
  });

  it("escapes both quote kinds so a value cannot leave an attribute", () => {
    expect(escapeHtml(`" onmouseover="x`)).toBe("&quot; onmouseover=&quot;x");
    expect(escapeHtml(`' onmouseover='x`)).toBe("&#39; onmouseover=&#39;x");
  });

  it("escapes & first, so entities are not double-decoded", () => {
    expect(escapeHtml("&lt;")).toBe("&amp;lt;");
  });

  it("html`` escapes interpolations in text context", () => {
    expect(html`<b>${"<i>"}</b>`.toString()).toBe("<b>&lt;i&gt;</b>");
  });

  it("html`` escapes interpolations in double and single quoted attributes", () => {
    const evil = `x" onerror="y' z`;
    expect(html`<a title="${evil}">`.toString()).toBe(`<a title="x&quot; onerror=&quot;y&#39; z">`);
    expect(html`<a title='${evil}'>`.toString()).toBe(`<a title='x&quot; onerror=&quot;y&#39; z'>`);
  });

  it("html`` does not escape nested html`` or raw(), and escapes their absence cleanly", () => {
    const inner = html`<i>${"<"}</i>`;
    expect(html`<p>${inner}${raw("<br>")}</p>`.toString()).toBe("<p><i>&lt;</i><br></p>");
    expect(html`${null}${undefined}${false}|${0}`.toString()).toBe("|0");
  });

  it("html`` joins arrays of fragments and escapes plain strings in them", () => {
    expect(html`<ul>${["<a>", html`<li>${"&"}</li>`]}</ul>`.toString()).toBe("<ul>&lt;a&gt;<li>&amp;</li></ul>");
  });
});
