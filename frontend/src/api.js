// Thin API client. In dev, Vite proxies /api -> http://localhost:8000 (see vite.config.js).
const BASE = "/api/v1";

async function jpost(path, body) {
  const r = await fetch(BASE + path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
  return r.json();
}

// sample PDF -> extract -> approve template -> register datasets -> suggest mapping
export const bootstrap = () => jpost("/demo/bootstrap");

// approve the suggestion (rejecting the given edge ids) -> { mapping_id, mapping }
export const approveSuggestion = (suggestionId, rejectEdgeIds) =>
  jpost(`/mappings/suggestions/${suggestionId}/approve`, {
    reject_edge_ids: rejectEdgeIds, approver: "canvas-user", name: "canvas-mapping",
  });

// run reconciliation -> run record incl. summary
export const runRecon = (body) => jpost("/recon/runs", body);
