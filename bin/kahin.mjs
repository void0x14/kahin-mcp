#!/usr/bin/env node
// kahin global launcher — gömülü wheel'den kahin MCP server'ı çalıştırır.
// Kurulum: pnpm add -g @kahinmcp/kahin  (postinstall venv + wheel kurar)

import { spawn } from "node:child_process";
import { existsSync } from "node:fs";
import { homedir } from "node:os";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const PACKAGE_ROOT = dirname(dirname(fileURLToPath(import.meta.url)));
// A local pnpm link can point PACKAGE_ROOT at a checkout that contains a
// ``kahin/`` source directory. Running ``python -m kahin.oracle`` with that
// checkout as cwd shadows the wheel installed in the launcher's venv and
// makes the same MCP command behave differently depending on the caller's
// cwd. Use an empty system temp cwd for that development-link case; published
// npm packages have no source directory and can run from their package root.
const PYTHON_CWD = existsSync(join(PACKAGE_ROOT, "kahin")) ? tmpdir() : PACKAGE_ROOT;

const HOME = process.env.KAHIN_HOME || join(homedir(), ".local", "share", "kahin");
const VENV = join(HOME, "venv");
const PY = process.platform === "win32" ? join(VENV, "Scripts", "python.exe") : join(VENV, "bin", "python");

function sh(cmd, args, opts = {}) {
  return new Promise((resolve) => {
    const c = spawn(cmd, args, { stdio: ["ignore", "ignore", "pipe"], ...opts });
    c.on("error", (err) => {
      process.stderr.write(`[kahin] ${cmd} başlatılamadı: ${err.message}\n`);
      resolve(1);
    });
    c.stderr.on("data", (d) => process.stderr.write(`[kahin] ${d}`));
    c.on("close", (code) => resolve(code));
  });
}

async function checkImport(python, source) {
  if (source === "editable") {
    return sh(
      python,
      [
        "-c",
        "import pathlib, sys, kahin, kahin.oracle; " +
          "assert pathlib.Path(kahin.__file__).resolve().is_relative_to(pathlib.Path(sys.argv[1]).resolve())",
        PACKAGE_ROOT,
      ],
      {
        stdio: ["ignore", "pipe", "pipe"],
        cwd: PYTHON_CWD,
      },
    );
  }
  return sh(python, ["-c", "import kahin.oracle"], {
    stdio: ["ignore", "pipe", "pipe"],
    cwd: PYTHON_CWD,
  });
}

async function setup() {
  // venv yoksa veya editable/wheel kaynağı değiştiyse ortamı eşitle
  const { installPython, resolveInstallSource } = await import("./setup.mjs");
  const source = resolveInstallSource();
  const res = await installPython();
  if (res && res.error) {
    process.stderr.write(`[kahin] python kurulumu başarısız (çıkış ${res.error})\n`);
    process.exit(1);
  }
  const python = existsSync(PY) ? PY : (process.env.KAHIN_PY || "python3");
  let check = await checkImport(python, source);
  if (check !== 0) {
    // Tek seferlik kendi kendini onarma: eksik bağımlılık veya stale wheel shadowing durumunda zorla yeniden eşitle.
    const healed = await installPython({ force: true });
    if (healed && healed.error) {
      process.stderr.write(`[kahin] python ortamı onarılamadı (çıkış ${healed.error})\n`);
      process.exit(1);
    }
    check = await checkImport(python, source);
  }
  if (check !== 0) {
    process.stderr.write(`[kahin] kahin.oracle paketi doğrulanamadı (${source}). ${python} ile çalıştırılamıyor.\n`);
    process.exit(1);
  }
  return python;
}

const args = process.argv.slice(2);

if (args[0] === "setup") {
  const { setup: registerClients } = await import("./setup.mjs");
  registerClients();
  const { installPython } = await import("./setup.mjs");
  const install = await installPython();
  if (install && install.error) {
    process.stderr.write(`[kahin] Python/Camoufox kurulumu başarısız (çıkış ${install.error})\n`);
    process.exit(1);
  }
  process.exit(0);
}

const python = await setup();
const child = spawn(python, ["-m", "kahin.oracle", ...args], {
  stdio: "inherit",
  cwd: PYTHON_CWD,
});
child.on("exit", (code, signal) => {
  if (signal) process.kill(process.pid, signal);
  process.exit(code ?? 0);
});
