/** Origin every dashboard request goes to: relative under `vite dev` (its proxy), the page origin in production. */
export const BASE_URL: string = import.meta.env.DEV ? "" : globalThis.location.origin;
