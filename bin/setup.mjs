#!/usr/bin/env node
// kahin auto-setup — kurulu AI CLI istemcilerini tespit eder ve kahin MCP'yi kaydeder.
// Idempotent: zaten kayıtlıysa atlar, mevcut config'leri merge eder (ezmez).
// Opt-out: KAHIN_SKIP_AUTO_SETUP=1
// El ile: kahin setup
//
// Client listesi, otomatik tespit yapan sistemlerden derlendi (add-mcp 15 ajan,
// everymcp 15, mcpm 20+, getmcp 19, mcp-get 9, mcpkit): 22 client.

import { existsSync, readFileSync, writeFileSync, mkdirSync, statSync } from "node:fs";
import { homedir } from "node:os";
import { join, dirname } from "node:path";
import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";

const HOME = homedir();
const isWin = process.platform === "win32";
const isMac = process.platform === "darwin";
const APPDATA = process.env.APPDATA || join(HOME, "AppData", "Roaming");

const ENTRY = { command: "kahin", args: [] };
const ENTRY_OPENCODE = { type: "local", command: ["kahin"], enabled: true };
const ENTRY_VSCODE = { type: "stdio", command: "kahin", args: [] };

// VS Code uzantı globalStorage yolu (Cline/Roo/Kilo burada yaşar)
function vscodeStorage(...ext) {
  const base = isWin
    ? join(APPDATA, "Code", "User", "globalStorage")
    : isMac
      ? join(HOME, "Library", "Application Support", "Code", "User", "globalStorage")
      : join(HOME, ".config", "Code", "User", "globalStorage");
  return join(base, ...ext);
}

// her client: { name, file, root, entry, format }
const CLIENTS = [
  // ---- JSON: root "mcpServers" ----
  {
    name: "Claude Code",
    file: join(HOME, ".claude.json"),
    root: "mcpServers",
  },
  {
    name: "Claude Desktop",
    file: isWin
      ? join(APPDATA, "Claude", "claude_desktop_config.json")
      : join(HOME, ".config", "Claude", "claude_desktop_config.json"),
    root: "mcpServers",
  },
  {
    name: "Cursor",
    file: join(HOME, ".cursor", "mcp.json"),
    root: "mcpServers",
  },
  {
    name: "Windsurf",
    file: join(HOME, ".codeium", "windsurf", "mcp_config.json"),
    root: "mcpServers",
  },
  {
    name: "Gemini CLI",
    file: join(HOME, ".gemini", "settings.json"),
    root: "mcpServers",
  },
  {
    name: "Cline (VS Code)",
    file: vscodeStorage("saoudrizwan.claude-dev", "settings", "cline_mcp_settings.json"),
    root: "mcpServers",
  },
  {
    name: "Cline CLI",
    file: join(HOME, ".cline", "data", "settings", "cline_mcp_settings.json"),
    root: "mcpServers",
  },
  {
    name: "Roo Code",
    file: vscodeStorage("rooveterinaryinc.roo-cline", "settings", "mcp_settings.json"),
    root: "mcpServers",
  },
  {
    name: "Kilo Code",
    file: vscodeStorage("kilocode.kilo-code", "settings", "mcp_settings.json"),
    root: "mcpServers",
  },
  {
    name: "Continue",
    file: join(HOME, ".continue", "config.json"),
    root: "mcpServers",
  },
  {
    name: "Amazon Q",
    file: join(HOME, ".aws", "amazonq", "mcp.json"),
    root: "mcpServers",
  },
  {
    name: "Trae",
    file: join(HOME, ".trae", "mcp.json"),
    root: "mcpServers",
  },
  {
    name: "BoltAI",
    file: isMac
      ? join(HOME, "Library", "Application Support", "boltAI", "config.json")
      : join(HOME, ".boltai", "config.json"),
    root: "mcpServers",
  },
  {
    name: "Antigravity",
    file: join(HOME, ".gemini", "config", "mcp_config.json"),
    root: "mcpServers",
  },
  {
    name: "Amp",
    file: join(HOME, ".amp", "config.json"),
    root: "mcpServers",
  },
  {
    name: "MCPorter",
    file: join(HOME, ".mcporter", "mcporter.json"),
    root: "mcpServers",
  },
  {
    name: "GitHub Copilot CLI",
    file: join(HOME, ".copilot", "mcp-config.json"),
    root: "mcpServers",
  },
  // ---- JSON: özel root ----
  {
    name: "Zed",
    file: isWin
      ? join(APPDATA, "Zed", "settings.json")
      : isMac
        ? join(HOME, "Library", "Application Support", "Zed", "settings.json")
        : join(HOME, ".config", "zed", "settings.json"),
    root: "context_servers",
  },
  {
    name: "opencode",
    file: join(HOME, ".config", "opencode", "opencode.json"),
    root: "mcp",
    entry: ENTRY_OPENCODE,
  },
  {
    name: "VS Code",
    file: isWin
      ? join(APPDATA, "Code", "User", "mcp.json")
      : isMac
        ? join(HOME, "Library", "Application Support", "Code", "User", "mcp.json")
        : join(HOME, ".config", "Code", "User", "mcp.json"),
    root: "servers",
    entry: ENTRY_VSCODE,
  },
  // ---- TOML ----
  {
    name: "Codex CLI",
    file: join(HOME, ".codex", "config.toml"),
    root: "mcp_servers",
    format: "toml",
  },
  // ---- YAML ----
  {
    name: "Goose",
    file: join(HOME, ".config", "goose", "config.yaml"),
    root: "extensions",
    format: "yaml",
  },
];

function log(msg) {
  process.stderr.write(`[kahin] ${msg}\n`);
}

// JSONC (yorumlu JSON) için basit strip — // ve /* */ satırları temizler.
function stripJsonc(src) {
  let out = "";
  let i = 0;
  const n = src.length;
  let inStr = false;
  while (i < n) {
    const c = src[i];
    const nx = src[i + 1];
    if (inStr) {
      out += c;
      if (c === "\\") {
        out += nx ?? "";
        i += 2;
        continue;
      }
      if (c === '"') inStr = false;
      i++;
      continue;
    }
    if (c === '"') {
      inStr = true;
      out += c;
      i++;
      continue;
    }
    if (c === "/" && nx === "/") {
      while (i < n && src[i] !== "\n") i++;
      continue;
    }
    if (c === "/" && nx === "*") {
      i += 2;
      while (i < n && !(src[i] === "*" && src[i + 1] === "/")) i++;
      i += 2;
      continue;
    }
    out += c;
    i++;
  }
  return out;
}

function readJson(file) {
  try {
    return JSON.parse(stripJsonc(readFileSync(file, "utf8")));
  } catch {
    return undefined;
  }
}

function upsertJson(client) {
  const { file, root, name } = client;
  const entry = client.entry || ENTRY;
  const cfg = readJson(file);
  if (cfg === undefined) {
    log(`${name}: config okunamadı (bozuk JSON?), atlandı`);
    return false;
  }
  if (typeof cfg !== "object" || cfg === null || Array.isArray(cfg)) {
    log(`${name}: beklenmeyen config yapısı, atlandı`);
    return false;
  }
  const rootObj = cfg[root];
  if (rootObj !== undefined && typeof rootObj !== "object") {
    log(`${name}: beklenmeyen "${root}" yapısı, atlandı`);
    return false;
  }
  cfg[root] = cfg[root] || {};
  if (cfg[root]["kahin"]) {
    log(`${name}: zaten kayıtlı, atlandı`);
    return false;
  }
  cfg[root]["kahin"] = entry;
  writeFileSync(file, JSON.stringify(cfg, null, 2) + "\n");
  log(`${name}: kaydedildi (${file})`);
  return true;
}

function upsertToml(client) {
  const { file, name } = client;
  let content = "";
  try {
    content = readFileSync(file, "utf8");
  } catch {
    log(`${name}: config okunamadı, atlandı`);
    return false;
  }
  if (content.includes("[mcp_servers.kahin]")) {
    log(`${name}: zaten kayıtlı, atlandı`);
    return false;
  }
  content += `\n[mcp_servers.kahin]\ncommand = "kahin"\nargs = []\n`;
  writeFileSync(file, content);
  log(`${name}: kaydedildi (${file})`);
  return true;
}

function upsertYaml(client) {
  const { file, name } = client;
  let content = "";
  try {
    content = readFileSync(file, "utf8");
  } catch {
    log(`${name}: config okunamadı, atlandı`);
    return false;
  }
  // extensions altında kahin bloğu var mı?
  if (/^extensions:\s*$[\s\S]*?^  kahin:/m.test(content)) {
    log(`${name}: zaten kayıtlı, atlandı`);
    return false;
  }
  if (!/^extensions:/m.test(content)) {
    content += "\nextensions:\n";
  }
  content += "  kahin:\n    cmd: kahin\n    enabled: true\n";
  writeFileSync(file, content);
  log(`${name}: kaydedildi (${file})`);
  return true;
}

export function setup() {
  if (process.env.KAHIN_SKIP_AUTO_SETUP || process.env.CI) {
    log("otomatik kurulum atlandı (KAHIN_SKIP_AUTO_SETUP/CI)");
    return { skipped: true };
  }
  let installed = 0;
  let found = 0;
  for (const client of CLIENTS) {
    if (!existsSync(client.file)) continue;
    found++;
    mkdirSync(dirname(client.file), { recursive: true });
    const ok =
      client.format === "toml"
        ? upsertToml(client)
        : client.format === "yaml"
          ? upsertYaml(client)
          : upsertJson(client);
    if (ok) installed++;
  }
  if (installed === 0) {
    if (found > 0) {
      log("tüm tespit edilen istemcilerde kahin zaten kayıtlı.");
    } else {
      log("desteklenen AI CLI aracı bulunamadı. `kahin setup` ile sonradan çalıştır.");
    }
  } else {
    log(`tamamlandı: ${installed} istemciye kaydedildi. Yeni kurulum sonrası aracı yeniden başlat.`);
  }
  return { installed };
}

const KAHIN_HOME = process.env.KAHIN_HOME || join(homedir(), ".local", "share", "kahin");
const KAHIN_VENV = join(KAHIN_HOME, "venv");
const KAHIN_PY = process.platform === "win32" ? join(KAHIN_VENV, "Scripts", "python.exe") : join(KAHIN_VENV, "bin", "python");
const KAHIN_WHEEL = join(dirname(fileURLToPath(import.meta.url)), "..", "lib", "kahin-0.3.10-py3-none-any.whl");
const KAHIN_INSTALL_MARKER = join(KAHIN_HOME, ".install-state.json");

// package root: `kahin/` lives next to bin/ in the checkout and inside the wheel.
const PACKAGE_ROOT = join(dirname(fileURLToPath(import.meta.url)), "..");
// QuickJS upstream for the nox host build input (never tracked in the repo).
const QUICKJS_REPO = "https://github.com/quickjs-ng/quickjs.git";
const SETUP_COMMAND_TIMEOUT_MS = 120_000;

function wheelStamp() {
  try {
    const info = statSync(KAHIN_WHEEL);
    return `${info.size}:${Math.trunc(info.mtimeMs)}`;
  } catch {
    return null;
  }
}

function installMarkerMatches(stamp) {
  if (!stamp || !existsSync(KAHIN_INSTALL_MARKER)) return false;
  try {
    const marker = JSON.parse(readFileSync(KAHIN_INSTALL_MARKER, "utf8"));
    return marker && marker.wheel === stamp && marker.python === KAHIN_PY;
  } catch {
    return false;
  }
}

function writeInstallMarker(stamp) {
  if (!stamp) return;
  try {
    mkdirSync(KAHIN_HOME, { recursive: true });
    writeFileSync(KAHIN_INSTALL_MARKER, JSON.stringify({ wheel: stamp, python: KAHIN_PY }) + "\n");
  } catch (err) {
    process.stderr.write(`[kahin] install marker yazılamadı: ${err.message}\n`);
  }
}

// nox add-on host binary'sini derler: Baidu ADAS `nox_jst_v1` çerezini üreten
// QuickJS host'u. Kaynak `kahin/addons/nox/noxhost.c` + `shim.js` repoda; QuickJS
// upstream'i build girdisi olarak indirilir (repayı şişirmez), sonuç
// `kahin/addons/nox/bin/` altına yazılır. Idempotent: binary varsa derlenmez.
//
// İsteğe bağlı: derleme araçları (cmake/cc) yoksa atlanır — nox kullanan araçlar
// `kahin_waf_cookie_*` bunu açıkça bildirir, WAF'sız siteler etkilenmez.
export async function buildNoxHost() {
  if (process.env.KAHIN_SKIP_NOX) {
    log("nox host derlemesi atlandı (KAHIN_SKIP_NOX)");
    return { skipped: true };
  }
  const addon = join(PACKAGE_ROOT, "kahin", "addons", "nox");
  const name = isWin ? "noxhost.exe" : "noxhost";
  const target = join(addon, "bin", name);
  if (existsSync(target)) {
    return { present: true };
  }
  const source = join(addon, "noxhost.c");
  const shim = join(addon, "shim.js");
  if (!existsSync(source) || !existsSync(shim)) {
    log("nox kaynakları eksik; derleme atlandı");
    return { skipped: true };
  }
  const quickjs = join(addon, "_build", "quickjs");
  if (!existsSync(join(quickjs, "CMakeLists.txt"))) {
    const code = await run("git", ["clone", "--depth", "1", QUICKJS_REPO, quickjs]);
    if (code !== 0 || !existsSync(join(quickjs, "CMakeLists.txt"))) {
      log(`nox: QuickJS kaynağı alınamadı (git çıkış ${code}) — nox cookie üretimi kapalı`);
      return { skipped: true };
    }
  }
  const cmake = await run("cmake", ["-B", join(quickjs, "build"), "-DCMAKE_BUILD_TYPE=Release", quickjs]);
  if (cmake !== 0) {
    log("nox: cmake başarısız — nox cookie üretimi kapalı");
    return { skipped: true };
  }
  const built = await run("cmake", ["--build", join(quickjs, "build"), "-j"]);
  if (built !== 0) {
    log("nox: QuickJS derlenemedi — nox cookie üretimi kapalı");
    return { skipped: true };
  }
  mkdirSync(join(addon, "bin"), { recursive: true });
  const cc = process.env.CC || "cc";
  const ccArgs = [
    "-O2", "-o", target, source,
    "-I", join(quickjs, "build"), "-I", quickjs,
    join(quickjs, "build", "libqjs.a"),
    "-lm", "-lpthread", "-ldl",
  ];
  const code = await run(cc, ccArgs, 180_000);
  if (code !== 0 || !existsSync(target)) {
    log(`nox: ${cc} derlemesi başarısız (çıkış ${code}) — nox cookie üretimi kapalı`);
    return { skipped: true };
  }
  log(`nox host derlendi: ${target}`);
  return { built: target };
}

// Gömülü wheel'i venv'e kurar ve varsayılan Camoufox binary'sini hazırlar.
// PyPI'a bağımlı DEĞİL — wheel paketle birlikte gelir; wheel'in bağımlılıkları
// (Camoufox dahil) pip tarafından kurulup resmi `camoufox fetch` ile tarayıcı
// cache'i doldurulur.
export async function installPython() {
  if (process.env.KAHIN_SKIP_PYTHON) {
    log("python kurulumu atlandı (KAHIN_SKIP_PYTHON)");
    return { skipped: true };
  }
  if (!existsSync(KAHIN_WHEEL)) {
    log(`wheel bulunamadı: ${KAHIN_WHEEL} — kurulum atlandı (geliştirme ortamı?)`);
    return { skipped: true };
  }
  const python3 = process.env.KAHIN_PYTHON || "python3";
  const args = ["-m", "venv", KAHIN_VENV];
  if (!existsSync(join(KAHIN_VENV, "pyvenv.cfg"))) {
    log(`venv oluşturuluyor: ${KAHIN_VENV}`);
    const code = await run(python3, args);
    if (code !== 0) {
      log(`venv oluşturulamadı (${python3} ${args.join(" ")} — çıkış ${code})`);
      return { error: code };
    }
  }
  const stamp = wheelStamp();
  if (installMarkerMatches(stamp)) {
    log("Kahin wheel zaten güncel; pip kurulumu atlandı");
  } else {
    log(`wheel kuruluyor: ${KAHIN_WHEEL}`);
    const code = await run(
      KAHIN_PY,
      ["-m", "pip", "install", "--upgrade", KAHIN_WHEEL],
      SETUP_COMMAND_TIMEOUT_MS,
    );
    if (code !== 0) {
      log(`wheel kurulamadı (çıkış ${code})`);
      return { error: code };
    }
    writeInstallMarker(stamp);
    log("kahin python paketi kuruldu/güncellendi");
  }

  if (process.env.KAHIN_SKIP_CAMOUFOX_FETCH) {
    log("Camoufox fetch atlandı (KAHIN_SKIP_CAMOUFOX_FETCH)");
    return { installed: true, camoufox: "skipped" };
  }
  const readyCode = await run(
    KAHIN_PY,
    ["-c", "from pathlib import Path; from camoufox.pkgman import launch_path; raise SystemExit(0 if Path(launch_path()).exists() else 1)"],
    15_000,
  );
  if (readyCode === 0) {
    log("Camoufox browser zaten hazır; fetch atlandı");
    return { installed: true, camoufox: "ready" };
  }
  log("Camoufox browser hazırlanıyor (resmi camoufox fetch)...");
  const browserCode = await run(KAHIN_PY, ["-m", "camoufox", "fetch"], SETUP_COMMAND_TIMEOUT_MS);
  if (browserCode !== 0) {
    log(`Camoufox browser hazırlanamadı (çıkış ${browserCode}); kahin browser_start sırasında tekrar deneyecek`);
    return { installed: true, camoufox: "error", error: browserCode };
  }
  log("Camoufox browser hazır");
  return { installed: true, camoufox: "ready" };
}

function run(cmd, args, timeoutMs = SETUP_COMMAND_TIMEOUT_MS) {
  return new Promise((resolve) => {
    const c = spawn(cmd, args, { stdio: ["ignore", "ignore", "pipe"] });
    let timedOut = false;
    let forceTimer;
    const timer = setTimeout(() => {
      timedOut = true;
      c.kill("SIGTERM");
      forceTimer = setTimeout(() => c.kill("SIGKILL"), 5_000);
    }, timeoutMs);
    c.on("error", (err) => {
      clearTimeout(timer);
      clearTimeout(forceTimer);
      process.stderr.write(`[kahin] ${cmd} başlatılamadı: ${err.message}\n`);
      resolve(1);
    });
    c.stderr.on("data", (d) => process.stderr.write(`[kahin] ${d}`));
    c.on("close", (code) => {
      clearTimeout(timer);
      clearTimeout(forceTimer);
      resolve(timedOut ? 124 : code);
    });
  });
}

if (process.argv[1] && process.argv[1].endsWith("setup.mjs")) {
  setup();
  await buildNoxHost();
  const result = await installPython();
  if (result?.error) process.exitCode = 1;
}
