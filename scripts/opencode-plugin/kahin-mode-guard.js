// Kahin durum-modu koruması — opencode eklentisi.
//
// Kahin başlatma çağrısının KENDİ argümanlarıyla çelişen bir `mode` taşımasını
// MCP aracı çalışmadan önce engeller. Görev metni OKUNMAZ; kelime tahmini yok.
// Engelleme throw ile olur; argümanlar asla yeniden yazılmaz.
//
// Kurallar kahin-mode-guard-core.mjs içinde (docs/state-modes.md §5.1).
// Her karar kahin-mode-guard.log dosyasına yazılır.

import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";

const LOG_PATH = path.join(
  os.homedir(),
  ".config",
  "opencode",
  "plugins",
  "kahin-mode-guard.log",
);

async function appendLog(line) {
  try {
    await fs.appendFile(LOG_PATH, `${new Date().toISOString()} ${line}\n`);
  } catch {
    // Günlük kararı etkilemez.
  }
}

let corePromise = null;
function loadCore() {
  if (!corePromise) {
    corePromise = import("./kahin-mode-guard-core.mjs").catch(async (error) => {
      await appendLog(`core-load-failed error=${error?.message ?? error}`);
      return null;
    });
  }
  return corePromise;
}

export const KahinModeGuardPlugin = async () => {
  return {
    "tool.execute.before": async (input, output) => {
      const tool = typeof input?.tool === "string" ? input.tool : "";
      // Core R0'ın aynısı; ilgisiz araçlar için çekirdeğe hiç gidilmez.
      if (!tool.toLowerCase().includes("browser_start")) return;

      const core = await loadCore();
      if (!core || typeof core.resolveGuard !== "function") return; // fail open

      const args = output?.args && typeof output.args === "object" ? output.args : {};

      let decision;
      try {
        decision = core.resolveGuard({ tool, args });
      } catch (error) {
        await appendLog(`resolve-error tool=${tool} error=${error?.message ?? error}`);
        return; // fail open
      }

      await appendLog(
        `decision=${decision.action} rule=${decision.rule} source=opencode ` +
          `tool=${tool} mode=${JSON.stringify(args.mode)} ` +
          `correct=${decision.correctMode ?? "-"} evidence=${decision.evidence ?? "-"} ` +
          `session=${input?.sessionID ?? "-"}`,
      );

      if (decision.action === "block") {
        throw new Error(decision.reason);
      }
    },
  };
};

export default KahinModeGuardPlugin;
