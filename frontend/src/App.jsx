import React, { useCallback, useMemo, useState } from "react";
import ReactFlow, {
  Background, Controls, MiniMap, Handle, Position, useNodesState, useEdgesState,
} from "reactflow";
import sampleGraph from "./sampleGraph.json";
import { bootstrap, approveSuggestion, runRecon, getResults, exportUrl } from "./api";

/* ---------- custom node ---------- */
function FieldNode({ data }) {
  const t = data.ntype;
  return (
    <div className={`node ${t}`}>
      {t !== "PDF_FIELD" && <Handle type="target" position={Position.Left} />}
      <div className="k">{data.label}</div>
      <div className="t">{t.replace("_FIELD", "").toLowerCase()}</div>
      {t !== "DEST_FIELD" && <Handle type="source" position={Position.Right} />}
    </div>
  );
}
const nodeTypes = { fieldNode: FieldNode };

const STYLE = {
  SUGGESTED: { stroke: "#f59e0b", strokeWidth: 2, strokeDasharray: "6 4" },
  APPROVED: { stroke: "#10b981", strokeWidth: 2.5 },
  REJECTED: { stroke: "#ef4444", strokeWidth: 1.5, strokeDasharray: "2 4", opacity: 0.5 },
};

const toRfNodes = (g) => g.nodes.map((n) => ({
  id: n.id, type: "fieldNode", position: { x: n.x, y: n.y },
  data: { label: n.label, ntype: n.type, ref: n.ref }, draggable: true,
}));
const toRfEdges = (g, meta) => g.edges.map((e) => {
  const status = meta[e.id]?.review_status ?? e.review_status;
  return {
    id: e.id, source: e.from_node, target: e.to_node,
    animated: status === "SUGGESTED", style: STYLE[status],
    label: `${Math.round((e.confidence ?? 0) * 100)}%`,
    labelStyle: { fontSize: 10, fill: "#6b7280" },
  };
});

function materialize(graph, meta) {
  const refOf = {}; graph.nodes.forEach((n) => { refOf[n.id] = n.ref; });
  const byField = {};
  graph.edges.forEach((e) => {
    if ((meta[e.id]?.review_status ?? e.review_status) !== "APPROVED") return;
    if (e.kind === "PDF_TO_SOURCE") {
      const fk = refOf[e.from_node];
      byField[fk] = byField[fk] || { field_key: fk };
      byField[fk].src_col = refOf[e.to_node];
      byField[fk].is_key = byField[fk].is_key || e.is_key;
    } else if (e.kind === "SOURCE_TO_DEST") {
      const srcRef = refOf[e.from_node], dstCol = refOf[e.to_node];
      const fld = Object.values(byField).find((d) => d.src_col === srcRef);
      if (fld) { fld.dst_col = dstCol; fld.is_key = fld.is_key || e.is_key; }
    }
  });
  const b = Object.values(byField).filter((d) => d.src_col && d.dst_col);
  return {
    name: "ai-suggested", template_version: "1.0.0", expected_side: "SOURCE",
    key_pairs: b.filter((d) => d.is_key).map((d) => ({ field_key: d.field_key, src_col: d.src_col, dst_col: d.dst_col })),
    field_bindings: b.map((d) => ({ field_key: d.field_key, src_col: d.src_col, dst_col: d.dst_col })),
  };
}

const SUMMARY_FIELDS = [
  ["total_records", "Total"], ["match_count", "✅ Match"], ["mismatch_count", "❌ Mismatch"],
  ["missing_src", "Missing src"], ["missing_dst", "Missing dst"],
  ["duplicate_count", "🔁 Dup"], ["exception_count", "🚫 Exc"],
];

export default function App() {
  const [graph, setGraph] = useState(sampleGraph);
  const [meta, setMeta] = useState(() => Object.fromEntries(sampleGraph.edges.map((e) => [e.id, { ...e }])));
  const [nodes, setNodes, onNodesChange] = useNodesState(toRfNodes(sampleGraph));
  const [edges, setEdges, onEdgesChange] = useEdgesState(toRfEdges(sampleGraph, meta));
  const [selected, setSelected] = useState(null);
  const [api, setApi] = useState(null);          // ids from bootstrap (live mode)
  const [runSummary, setRunSummary] = useState(null);
  const [runId, setRunId] = useState(null);
  const [results, setResults] = useState([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  const applyGraph = useCallback((g) => {
    const m = Object.fromEntries(g.edges.map((e) => [e.id, { ...e }]));
    setGraph(g); setMeta(m); setNodes(toRfNodes(g)); setEdges(toRfEdges(g, m));
    setSelected(null); setRunSummary(null);
  }, [setNodes, setEdges]);

  const refresh = useCallback((next) => { setMeta(next); setEdges(toRfEdges(graph, next)); }, [graph, setEdges]);
  const setStatus = useCallback((id, s) => refresh({ ...meta, [id]: { ...meta[id], review_status: s } }), [meta, refresh]);
  const approveAll = () => refresh(Object.fromEntries(Object.entries(meta).map(([k, v]) => [k, { ...v, review_status: "APPROVED" }])));

  const counts = useMemo(() => {
    const c = { SUGGESTED: 0, APPROVED: 0, REJECTED: 0, low: 0 };
    Object.values(meta).forEach((e) => {
      c[e.review_status] = (c[e.review_status] || 0) + 1;
      if ((e.confidence ?? 1) < 0.7) c.low += 1;
    });
    return c;
  }, [meta]);

  const mapping = useMemo(() => materialize(graph, meta), [graph, meta]);
  const sel = selected ? meta[selected] : null;

  const loadFromApi = async () => {
    setBusy(true); setError(null);
    try {
      const data = await bootstrap();
      setApi({
        suggestion_id: data.suggestion_id, template_id: data.template_id,
        code_lists_id: data.code_lists_id, source_dataset_id: data.source_dataset_id,
        dest_dataset_id: data.dest_dataset_id, as_of_date: data.as_of_date,
      });
      applyGraph(data.graph);
    } catch (e) { setError(`Load failed: ${e.message}. Is the API running on :8000?`); }
    finally { setBusy(false); }
  };

  const reconcile = async () => {
    if (!api) return;
    setBusy(true); setError(null);
    try {
      const rejectIds = Object.entries(meta)
        .filter(([, e]) => e.review_status === "REJECTED").map(([id]) => id);
      const appr = await approveSuggestion(api.suggestion_id, rejectIds);
      const run = await runRecon({
        template_id: api.template_id, mapping_id: appr.mapping_id,
        code_lists_id: api.code_lists_id, source_dataset_id: api.source_dataset_id,
        dest_dataset_id: api.dest_dataset_id, as_of_date: api.as_of_date,
      });
      setRunSummary(run.summary);
      setRunId(run.run_id);
      const res = await getResults(run.run_id);
      setResults(res.items.filter((r) => r.category !== "MATCH"));
    } catch (e) { setError(`Reconcile failed: ${e.message}`); }
    finally { setBusy(false); }
  };

  const download = () => {
    const blob = new Blob([JSON.stringify(mapping, null, 2)], { type: "application/json" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob); a.download = "mapping.json"; a.click();
  };

  return (
    <div className="app">
      <div className="toolbar">
        <h1>Validation Center · Mapping Canvas</h1>
        <span className="chip suggested">suggested {counts.SUGGESTED}</span>
        <span className="chip approved">approved {counts.APPROVED}</span>
        <span className="chip rejected">rejected {counts.REJECTED}</span>
        {counts.low > 0 && <span className="chip low">⚠ low-conf {counts.low}</span>}
        <div className="spacer" />
        <button onClick={loadFromApi} disabled={busy}>Load from API</button>
        <button onClick={approveAll}>Approve all</button>
        <button onClick={download} disabled={mapping.key_pairs.length === 0}>Export</button>
        <button className="primary" onClick={reconcile} disabled={busy || !api}>
          {busy ? "…" : "Approve & reconcile"}
        </button>
      </div>

      <div className="main">
        <div className="canvas">
          <ReactFlow
            nodes={nodes} edges={edges}
            onNodesChange={onNodesChange} onEdgesChange={onEdgesChange}
            nodeTypes={nodeTypes} onEdgeClick={(_, e) => setSelected(e.id)} fitView>
            <Background /><Controls /><MiniMap pannable zoomable />
          </ReactFlow>
        </div>

        <div className="sidebar">
          {error && <div className="chip rejected" style={{ display: "block", marginBottom: 10 }}>{error}</div>}
          {runSummary && <div style={{ marginBottom: 12 }}>
            <h2>Reconciliation result</h2>
            {SUMMARY_FIELDS.map(([k, label]) => (
              <div className="kv" key={k}><b>{label}</b>{runSummary[k]}</div>
            ))}
            {runId && <a className="dl" href={exportUrl(runId)} target="_blank" rel="noreferrer">
              ⬇ Download Excel report</a>}
            {results.length > 0 && <>
              <h2 style={{ marginTop: 12 }}>Issues ({results.length})</h2>
              <table className="results">
                <thead><tr><th>Key</th><th>Field</th><th>Cat</th><th>Actual</th></tr></thead>
                <tbody>
                  {results.map((r, i) => (
                    <tr key={i} className={`cat-${r.category}`}>
                      <td title={r.record_key}>{r.record_key.split("|")[0]}</td>
                      <td>{r.field_key}</td>
                      <td>{r.category === "EXCEPTION" ? "EXC" : r.severity?.[0] || "?"}</td>
                      <td title={r.verdict}>{r.actual}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </>}
          </div>}

          {!sel && <>
            <h2>Mapping preview {api ? "(live)" : "(sample)"}</h2>
            <div className="kv"><b>keys</b>{mapping.key_pairs.map((k) => k.field_key).join(", ") || "—"}</div>
            <div className="kv"><b>bindings</b>{mapping.field_bindings.length}</div>
            <p className="hint">
              {api ? "Reject any wrong edges, then Approve & reconcile."
                   : "Showing a bundled suggestion. Click 'Load from API' to drive the live backend."}
            </p>
            <pre>{JSON.stringify(mapping, null, 2)}</pre>
          </>}

          {sel && <>
            <h2>Edge review</h2>
            <div className="kv"><b>kind</b>{sel.kind}</div>
            <div className="kv"><b>status</b>
              <span className={`chip ${sel.review_status.toLowerCase()}`}>{sel.review_status}</span></div>
            <div className="kv"><b>key field</b>{sel.is_key ? "yes" : "no"}</div>
            <div className="kv"><b>confidence</b>{Math.round((sel.confidence ?? 0) * 100)}%
              <div className="bar"><i style={{ width: `${(sel.confidence ?? 0) * 100}%` }} /></div></div>
            <div className="kv"><b>reasoning</b></div>
            <div className="reason">{sel.reasoning || "—"}</div>
            <div className="edge-actions">
              <button className="approve" onClick={() => setStatus(selected, "APPROVED")}>Approve</button>
              <button className="reject" onClick={() => setStatus(selected, "REJECTED")}>Reject</button>
            </div>
            <p className="hint" style={{ marginTop: 12, cursor: "pointer" }}
              onClick={() => setSelected(null)}>← back to mapping preview</p>
          </>}
        </div>
      </div>
    </div>
  );
}
