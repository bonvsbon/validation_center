import React, { useCallback, useMemo, useState } from "react";
import ReactFlow, {
  Background, Controls, MiniMap, Handle, Position, useNodesState, useEdgesState,
} from "reactflow";
import sampleGraph from "./sampleGraph.json";

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

/* ---------- edge styling by review status ---------- */
const STYLE = {
  SUGGESTED: { stroke: "#f59e0b", strokeWidth: 2, strokeDasharray: "6 4" },
  APPROVED: { stroke: "#10b981", strokeWidth: 2.5 },
  REJECTED: { stroke: "#ef4444", strokeWidth: 1.5, strokeDasharray: "2 4", opacity: 0.5 },
};

function toRfNodes(g) {
  return g.nodes.map((n) => ({
    id: n.id, type: "fieldNode", position: { x: n.x, y: n.y },
    data: { label: n.label, ntype: n.type, ref: n.ref },
    draggable: true,
  }));
}
function toRfEdges(g, meta) {
  return g.edges.map((e) => {
    const status = meta[e.id]?.review_status ?? e.review_status;
    return {
      id: e.id, source: e.from_node, target: e.to_node,
      animated: status === "SUGGESTED",
      style: STYLE[status],
      label: `${Math.round((e.confidence ?? 0) * 100)}%`,
      labelStyle: { fontSize: 10, fill: "#6b7280" },
    };
  });
}

/* ---------- materialize resolver mapping from APPROVED edges ---------- */
function materialize(graph, meta) {
  const refOf = {}, typeOf = {};
  graph.nodes.forEach((n) => { refOf[n.id] = n.ref; typeOf[n.id] = n.type; });
  const byField = {};
  graph.edges.forEach((e) => {
    const status = meta[e.id]?.review_status ?? e.review_status;
    if (status !== "APPROVED") return;
    if (e.kind === "PDF_TO_SOURCE") {
      const fk = refOf[e.from_node];
      byField[fk] = byField[fk] || { field_key: fk };
      byField[fk].src_col = refOf[e.to_node];
      byField[fk].is_key = byField[fk].is_key || e.is_key;
    } else if (e.kind === "SOURCE_TO_DEST") {
      const srcRef = refOf[e.from_node];
      const dstCol = refOf[e.to_node];
      const fld = Object.values(byField).find((d) => d.src_col === srcRef);
      if (fld) { fld.dst_col = dstCol; fld.is_key = fld.is_key || e.is_key; }
    }
  });
  const bindings = Object.values(byField).filter((d) => d.src_col && d.dst_col);
  return {
    name: "ai-suggested", template_version: "1.0.0", expected_side: "SOURCE",
    key_pairs: bindings.filter((d) => d.is_key)
      .map((d) => ({ field_key: d.field_key, src_col: d.src_col, dst_col: d.dst_col })),
    field_bindings: bindings.map((d) => ({ field_key: d.field_key, src_col: d.src_col, dst_col: d.dst_col })),
  };
}

export default function App() {
  const graph = sampleGraph;
  const [meta, setMeta] = useState(() =>
    Object.fromEntries(graph.edges.map((e) => [e.id, { ...e }])));
  const [nodes, , onNodesChange] = useNodesState(toRfNodes(graph));
  const [edges, setEdges, onEdgesChange] = useEdgesState(toRfEdges(graph, meta));
  const [selected, setSelected] = useState(null);

  const refresh = useCallback((nextMeta) => {
    setMeta(nextMeta);
    setEdges(toRfEdges(graph, nextMeta));
  }, [graph, setEdges]);

  const setStatus = useCallback((id, status) => {
    const next = { ...meta, [id]: { ...meta[id], review_status: status } };
    refresh(next);
  }, [meta, refresh]);

  const approveAll = () => refresh(
    Object.fromEntries(Object.entries(meta).map(([k, v]) => [k, { ...v, review_status: "APPROVED" }])));

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
        {counts.low > 0 && <span className="chip low">⚠ low-confidence {counts.low}</span>}
        <div className="spacer" />
        <button onClick={approveAll}>Approve all</button>
        <button className="primary" onClick={download}
          disabled={mapping.key_pairs.length === 0}>Export mapping</button>
      </div>

      <div className="main">
        <div className="canvas">
          <ReactFlow
            nodes={nodes} edges={edges}
            onNodesChange={onNodesChange} onEdgesChange={onEdgesChange}
            nodeTypes={nodeTypes}
            onEdgeClick={(_, e) => setSelected(e.id)}
            fitView>
            <Background />
            <Controls />
            <MiniMap pannable zoomable />
          </ReactFlow>
        </div>

        <div className="sidebar">
          {!sel && <>
            <h2>Mapping preview</h2>
            <div className="kv"><b>keys</b>{mapping.key_pairs.map((k) => k.field_key).join(", ") || "—"}</div>
            <div className="kv"><b>bindings</b>{mapping.field_bindings.length}</div>
            <p className="hint">Click an edge to review the AI suggestion, then approve or reject it.
              Only approved edges become the mapping.</p>
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
            <p className="hint" style={{ marginTop: 12 }}
              onClick={() => setSelected(null)}>← back to mapping preview</p>
          </>}
        </div>
      </div>
    </div>
  );
}
