// Kahin durum-modu koruması — SADECE argümanlara bakar.
//
// Karar, çağrının KENDİ argümanlarındaki bir çelişkiye dayanır. Görev metni
// okunmaz, kelime tahmini yapılmaz.
//
// Kurallar:
//   R0  araç Kahin başlatma aracı değil            -> izin
//   R1  `mode` verilmemiş                          -> izin (Kahin mode_required der)
//   R2  passkey_mode=true | profile_dir | addons
//       ağırbaş gerektirir                         -> mode keş ise engelle
//   R3  çelişki yok                                -> izin
//
// Açık uçlu (fail open): beklenmeyen durumda geçirir. Engelleme throw ile olur;
// argümanlar asla yeniden yazılmaz.

export const MODE_AGIRBAS = "ağırbaş";
export const MODE_KES = "keş";

// ASCII yazımlar da kabul edilir (docs/state-modes.md §2.5).
const MODE_ALIASES = new Map([
  ["ağırbaş", MODE_AGIRBAS],
  ["agirbas", MODE_AGIRBAS],
  ["keş", MODE_KES],
  ["kes", MODE_KES],
]);

// Türkçe katlama "GİRİŞ" gibi yazımları eşler.
function fold(value) {
  return typeof value === "string" ? value.toLocaleLowerCase("tr") : "";
}

// Düz katlama. Türkçe katlama ASCII "I"yı noktasız "ı"ya çevirdiği için
// "AGIRBAS" tek başına eşleşmez; iki katlama da denenir.
function foldAscii(value) {
  return typeof value === "string" ? value.toLowerCase() : "";
}

/** Araç adı Kahin başlatma aracına benziyor mu. */
export function isKahinStartTool(tool) {
  return typeof tool === "string" && tool.toLowerCase().includes("browser_start");
}

/** Kabul edilen her yazımı kanonik moda çevirir; bilinmeyende null. */
export function normalizeMode(mode) {
  if (typeof mode !== "string") return null;
  const trimmed = mode.trim();
  return MODE_ALIASES.get(fold(trimmed)) ?? MODE_ALIASES.get(foldAscii(trimmed)) ?? null;
}

function modeIsMissing(args) {
  return args.mode === undefined || args.mode === null || args.mode === "";
}

// R2: hangi argüman kalıcı (ağırbaş) profil gerektirir.
function machineEvidence(args) {
  if (args.passkey_mode === true) return "passkey_mode=true";
  const profileDir = args.profile_dir;
  if (
    profileDir !== undefined &&
    profileDir !== null &&
    !(typeof profileDir === "string" && profileDir.trim() === "")
  ) {
    return "profile_dir";
  }
  if (Array.isArray(args.addons) && args.addons.length > 0) return "addons";
  return null;
}

function allow(rule, reason) {
  return { action: "allow", rule, reason };
}

function block(correctMode, evidence, rule, givenMode) {
  return {
    action: "block",
    correctMode,
    evidence,
    rule,
    reason:
      `[kahin-mode-guard] mode=${JSON.stringify(givenMode)} contradicts the call's own ` +
      `arguments (${evidence}); the correct mode is '${correctMode}'. ` +
      `Re-call kahin_browser_start with mode='${correctMode}'.`,
  };
}

/** Bir Kahin başlatma çağrısı için kararı üretir. */
export function resolveGuard(input) {
  const { tool, args } = input ?? {};
  const a = args && typeof args === "object" ? args : {};

  if (!isKahinStartTool(tool)) return allow("R0", "tool is not the Kahin start tool");
  if (modeIsMissing(a)) return allow("R1", "mode absent; Kahin returns mode_required");

  const requested = normalizeMode(a.mode);
  const evidence = machineEvidence(a);
  if (evidence && requested === MODE_KES) return block(MODE_AGIRBAS, evidence, "R2", a.mode);
  return allow("R3", "no argument contradiction");
}

export default resolveGuard;
