const express = require("express");
const path = require("path");
const { createProxyMiddleware } = require("http-proxy-middleware");

const app = express();
const PORT = 3000;
const BACKEND = "http://localhost:8001";

// ─── Proxy: REST API calls (/api/* → backend) ────────────────────────────────
app.use(
  "/api",
  createProxyMiddleware({
    target: BACKEND,
    changeOrigin: true,
    pathRewrite: { "^/api": "" }, // strip /api prefix
  })
);

// ─── Proxy: WebSocket (/ws/* → backend) ──────────────────────────────────────
const wsProxy = createProxyMiddleware({
  target: BACKEND,
  changeOrigin: true,
  ws: true,
});
app.use("/ws", wsProxy);

// ─── Static files ─────────────────────────────────────────────────────────────
app.use(express.static(path.join(__dirname, "public")));

// ─── Friendly route aliases ────────────────────────────────────────────────────
app.get("/recruiter", (req, res) =>
  res.sendFile(path.join(__dirname, "public", "recruiter.html"))
);
app.get("/candidate", (req, res) =>
  res.sendFile(path.join(__dirname, "public", "candidate.html"))
);
app.get("/results", (req, res) =>
  res.sendFile(path.join(__dirname, "public", "results.html"))
);

// ─── Start ─────────────────────────────────────────────────────────────────────
const server = app.listen(PORT, () => {
  console.log(`\n🚀  AI Interview Frontend  →  http://localhost:${PORT}`);
  console.log(`   Recruiter portal       →  http://localhost:${PORT}/recruiter`);
  console.log(`   Candidate portal       →  http://localhost:${PORT}/candidate`);
  console.log(`   API proxied to         →  ${BACKEND}\n`);
});

// Upgrade WebSocket connections through the proxy
server.on("upgrade", wsProxy.upgrade);
