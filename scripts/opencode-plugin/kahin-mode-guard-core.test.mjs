// Kahin mod koruması çekirdeği testleri — SADECE argüman kuralı.
//
// Görev metni / kelime tahmini kuralı kaldırıldı; burada onunla ilgili test yok.

import assert from "node:assert/strict";
import test from "node:test";

import {
  MODE_AGIRBAS,
  MODE_KES,
  isKahinStartTool,
  normalizeMode,
  resolveGuard,
} from "./kahin-mode-guard-core.mjs";

const ARAC = "kahin_kahin_browser_start";

// --- R0: araç filtresi -----------------------------------------------------

test("R0: Kahin başlatma aracı olmayan çağrı geçer", () => {
  for (const tool of ["read", "bash", "kahin_kahin_navigate", "", null, 7, undefined]) {
    assert.equal(resolveGuard({ tool, args: { mode: "keş" } }).action, "allow");
  }
});

test("isKahinStartTool yalnızca browser_start içeren adları tanır", () => {
  assert.equal(isKahinStartTool(ARAC), true);
  assert.equal(isKahinStartTool("kahin_browser_start"), true);
  assert.equal(isKahinStartTool("kahin_browser_stop"), false);
  assert.equal(isKahinStartTool(null), false);
});

// --- R1: mode yok ----------------------------------------------------------

test("R1: mode yoksa geçer (Kahin mode_required der)", () => {
  for (const mode of [undefined, null, ""]) {
    const v = resolveGuard({ tool: ARAC, args: { mode } });
    assert.equal(v.action, "allow");
    assert.equal(v.rule, "R1");
  }
});

// --- R2: argümandan gelen kesin kanıt --------------------------------------

test("R2: passkey_mode=true + keş -> engellenir, doğru mod ağırbaş", () => {
  const v = resolveGuard({ tool: ARAC, args: { mode: "keş", passkey_mode: true } });
  assert.equal(v.action, "block");
  assert.equal(v.correctMode, MODE_AGIRBAS);
  assert.equal(v.evidence, "passkey_mode=true");
});

test("R2: profile_dir dolu + keş -> engellenir", () => {
  const v = resolveGuard({ tool: ARAC, args: { mode: "kes", profile_dir: "/tmp/p" } });
  assert.equal(v.action, "block");
  assert.equal(v.evidence, "profile_dir");
});

test("R2: addons dolu + keş -> engellenir", () => {
  const v = resolveGuard({ tool: ARAC, args: { mode: "keş", addons: ["/tmp/a"] } });
  assert.equal(v.action, "block");
  assert.equal(v.evidence, "addons");
});

test("R2: boş profile_dir / boş addons kanıt sayılmaz", () => {
  assert.equal(resolveGuard({ tool: ARAC, args: { mode: "keş", profile_dir: "  " } }).action, "allow");
  assert.equal(resolveGuard({ tool: ARAC, args: { mode: "keş", addons: [] } }).action, "allow");
});

test("R2: kanıt varken ağırbaş seçilmişse geçer", () => {
  for (const args of [
    { mode: "ağırbaş", passkey_mode: true },
    { mode: "ağırbaş", profile_dir: "/tmp/p" },
    { mode: "ağırbaş", addons: ["/tmp/a"] },
  ]) {
    assert.equal(resolveGuard({ tool: ARAC, args }).action, "allow");
  }
});

// --- R3: çelişki yok -------------------------------------------------------

test("R3: argüman çelişkisi yoksa geçer", () => {
  for (const mode of ["keş", "ağırbaş", "agirbas", "kes", "AĞIRBAŞ", "AGIRBAS", "KES", "Keş"]) {
    const v = resolveGuard({ tool: ARAC, args: { mode } });
    assert.equal(v.action, "allow", mode);
  }
});

test("bilinmeyen mode değeri geçer (fail open)", () => {
  for (const mode of ["kalici", "persistent", 7, ["keş"], {}, true]) {
    assert.equal(resolveGuard({ tool: ARAC, args: { mode } }).action, "allow");
  }
});

// --- normalizeMode ---------------------------------------------------------

test("normalizeMode tüm kabul edilen yazımları kanonikleştirir", () => {
  assert.equal(normalizeMode("ağırbaş"), MODE_AGIRBAS);
  assert.equal(normalizeMode("agirbas"), MODE_AGIRBAS);
  assert.equal(normalizeMode("AĞIRBAŞ"), MODE_AGIRBAS);
  assert.equal(normalizeMode("AGIRBAS"), MODE_AGIRBAS);
  assert.equal(normalizeMode("  Agirbas  "), MODE_AGIRBAS);
  assert.equal(normalizeMode("keş"), MODE_KES);
  assert.equal(normalizeMode("kes"), MODE_KES);
  assert.equal(normalizeMode("KEŞ"), MODE_KES);
  assert.equal(normalizeMode("KES"), MODE_KES);
  assert.equal(normalizeMode("  Kes  "), MODE_KES);
  assert.equal(normalizeMode("bilinmeyen"), null);
  assert.equal(normalizeMode(null), null);
  assert.equal(normalizeMode(7), null);
});

// --- dayanıklılık ----------------------------------------------------------

test("bozuk girdi hata fırlatmaz", () => {
  for (const input of [undefined, null, {}, { tool: ARAC }, { tool: ARAC, args: null }, { tool: ARAC, args: 7 }]) {
    assert.doesNotThrow(() => resolveGuard(input));
  }
});
