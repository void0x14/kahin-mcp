// Kahin "devam" kancası — opencode.
//
// Oturum idle olduğunda aynı oturuma devam mesajı gönderir; böylece ajan
// "olmadı / yapamadım" deyip stream'i kesemez.
//
// Durma: sonsuz döngü yok. Devam yalnızca o oturumda AKTİF bir hedef (goal)
// varken gider. Hedef tamamlanınca/engellenince/durdurulunca kanca susar ve
// oturum normal biter.
//
// Hedef durumu (loop plugin'inin yazdığı yer):
//   <directory>/.opencode/opencode-loop/<sessionID>.json
//   -> { version, jobs: [ { kind:"goal", enabled, paused, goalStatus, ... } ] }
// Karar ölçütü loop plugin'inin kendi ölçütüyle AYNI olacak şekilde alındı
// (~/.config/opencode/plugins/opencode-loop.ts). Ancak `enabled`/`paused`
// KULLANILMIYOR: loop plugin'i her yeni mesajda `paused:true` yazdığı için
// kanca pratikte hiç ateşlemiyordu. Bitiş yalnızca goalStatus ile belirlenir.
//
// Mesaj gönderimi (SDK'dan doğrulandı):
//   client.session.promptAsync({ path:{id}, body:{ parts:[...] } })
//   POST /session/{id}/prompt_async -> 204

import fs from "node:fs/promises";
import path from "node:path";

const DEVAM_METNI =
  "DEVAM. İş bitmedi. Kaldığın yerden devam et. " +
  "Durma, özet isteyip çekilme, 'yapamadım' deme. Sıradaki adımı yap.";

const AYNI_OTURUM_BEKLEME_MS = 1000;
const SON_GONDERIM_UST_SINIR = 200;
const GUNLUK = "/tmp/kahin-devam.log";

// Loop plugin'inin "durdu" saydığı durumlar (aynı liste).
const DURMUS_SAYILAN = new Set(["completed", "blocked", "cleared"]);

async function gunluk(satir) {
  try {
    await fs.appendFile(GUNLUK, `${new Date().toISOString()} ${satir}\n`);
  } catch {
    // günlük yazılamazsa kanca davranışı değişmez
  }
}

async function hedefAktifMi(directory, sessionID) {
  try {
    const dosya = path.join(directory, ".opencode", "opencode-loop", `${sessionID}.json`);
    const veri = JSON.parse(await fs.readFile(dosya, "utf8"));
    const isler = Array.isArray(veri?.jobs) ? veri.jobs : [];
    const hedef = isler.find((is) => is && String(is.kind || "").toLowerCase() === "goal");
    if (!hedef) return false;
    // `enabled`/`paused` BİLEREK kullanılmıyor: loop plugin'i her yeni mesajda
    // `paused:true` yazıyor ("Paused because the user sent a new message..."),
    // bu yüzden kanca hiç ateşlemiyordu. "Kullanıcı konuşuyor" işin bittiği
    // anlamına gelmez; bitiş yalnızca goalStatus ile belirlenir.
    return !DURMUS_SAYILAN.has(String(hedef.goalStatus || ""));
  } catch {
    return false;
  }
}

export const KahinDevam = async ({ directory, worktree, client }) => {
  // Yazıcı (loop plugin) `directory` kullanıyor; kök onunla aynı olmalı.
  const kokDizin = directory || worktree || "";
  const sonGonderim = new Map();

  function eskit() {
    while (sonGonderim.size > SON_GONDERIM_UST_SINIR) {
      const ilk = sonGonderim.keys().next().value;
      sonGonderim.delete(ilk);
    }
  }

  async function devamEt(sessionID) {
    if (typeof sessionID !== "string" || !sessionID) return;
    if (!(await hedefAktifMi(kokDizin, sessionID))) return;
    const simdi = Date.now();
    const onceki = sonGonderim.get(sessionID) ?? 0;
    if (simdi - onceki < AYNI_OTURUM_BEKLEME_MS) return;
    sonGonderim.set(sessionID, simdi);
    eskit();
    try {
      await client.session.promptAsync({
        path: { id: sessionID },
        body: { parts: [{ type: "text", text: DEVAM_METNI }] },
      });
      await gunluk(`devam-gonderildi session=${sessionID} kok=${kokDizin}`);
    } catch (hata) {
      await gunluk(`devam-hatasi session=${sessionID} hata=${hata?.message ?? hata}`);
    }
  }

  return {
    event: async ({ event }) => {
      const tip = event?.type;
      const p = event?.properties ?? {};
      if (tip === "session.idle") return devamEt(p.sessionID);
      if (tip === "session.status" && p.status?.type === "idle") return devamEt(p.sessionID);
    },
  };
};

export default KahinDevam;
