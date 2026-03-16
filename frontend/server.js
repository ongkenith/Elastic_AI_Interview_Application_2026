const express = require("express");
const fs = require("fs");
const path = require("path");
const { createProxyMiddleware } = require("http-proxy-middleware");

function loadEnvFile(filePath) {
  if (!fs.existsSync(filePath)) {
    return;
  }

  const lines = fs.readFileSync(filePath, "utf8").split(/\r?\n/);
  for (const line of lines) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith("#")) {
      continue;
    }

    const equalsIndex = trimmed.indexOf("=");
    if (equalsIndex === -1) {
      continue;
    }

    const key = trimmed.slice(0, equalsIndex).trim();
    if (!key || process.env[key] !== undefined) {
      continue;
    }

    let value = trimmed.slice(equalsIndex + 1).trim();
    if (
      (value.startsWith('"') && value.endsWith('"')) ||
      (value.startsWith("'") && value.endsWith("'"))
    ) {
      value = value.slice(1, -1);
    }
    process.env[key] = value;
  }
}

const app = express();
loadEnvFile(path.resolve(__dirname, ".env"));
loadEnvFile(path.resolve(__dirname, "..", ".env"));

const PORT = Number(process.env.FRONTEND_PORT || 3000);
const BACKEND = (
  process.env.NGROK_BACKEND_URL ||
  process.env.BACKEND_URL ||
  "http://localhost:8001"
).replace(/\/$/, "");

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
app.get("/livecoding", (req, res) =>
  res.sendFile(path.join(__dirname, "public", "livecoding.html"))
);

// ─── Start ─────────────────────────────────────────────────────────────────────
const server = app.listen(PORT, () => {
  console.log(`\n🚀  AI Interview Frontend  →  http://localhost:${PORT}`);
  console.log(`   Recruiter portal       →  http://localhost:${PORT}/recruiter`);
  console.log(`   Candidate portal       →  http://localhost:${PORT}/candidate`);
  console.log(`   Live Coding portal     →  http://localhost:${PORT}/livecoding`);
  console.log(`   API proxied to         →  ${BACKEND}\n`);
});

// Upgrade WebSocket connections through the proxy
server.on("upgrade", wsProxy.upgrade);
