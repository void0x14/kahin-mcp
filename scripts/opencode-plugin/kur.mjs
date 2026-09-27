#!/usr/bin/env node
// Kahin opencode kancalarını kurar — makineden bağımsız.
//
// Ne yapar:
//   1) Bu dizindeki kanca dosyalarını ~/.config/opencode/plugins/ altına kopyalar
//   2) ~/.config/opencode/opencode.json içindeki `plugin` dizisine kaydeder
//
// Yol hesabı: os.homedir() ve OPENCODE_CONFIG_DIR. Sabit /home/... yolu YOK.
// Idempotent: tekrar çalıştırmak zarar vermez. opencode.json'un yedeği alınır.
//
// Kullanım:  node scripts/opencode-plugin/kur.mjs

import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const BURASI = path.dirname(fileURLToPath(import.meta.url));

const AYAR_DIR = process.env.OPENCODE_CONFIG_DIR
  ? path.resolve(process.env.OPENCODE_CONFIG_DIR)
  : path.join(os.homedir(), ".config", "opencode");
const EKLENTI_DIR = path.join(AYAR_DIR, "plugins");
const AYAR_DOSYA = path.join(AYAR_DIR, "opencode.json");

// Kopyalanacak dosyalar ve opencode.json'a yazılacak karşılıkları.
const KANCA_DOSYALARI = ["kahin-mode-guard-core.mjs", "kahin-mode-guard.js", "kahin-devam.js"];
const KAYITLAR = ["./plugins/kahin-mode-guard.js", "./plugins/kahin-devam.js"];

async function varMi(p) {
  try {
    await fs.access(p);
    return true;
  } catch {
    return false;
  }
}

async function main() {
  await fs.mkdir(EKLENTI_DIR, { recursive: true });

  // 1) kanca dosyalarını kopyala
  for (const ad of KANCA_DOSYALARI) {
    const kaynak = path.join(BURASI, ad);
    if (!(await varMi(kaynak))) {
      throw new Error(`kaynak yok: ${kaynak}`);
    }
    await fs.copyFile(kaynak, path.join(EKLENTI_DIR, ad));
    console.log(`kopyalandi  ${ad}`);
  }

  // 2) opencode.json'a kaydet
  if (!(await varMi(AYAR_DOSYA))) {
    console.log(`uyari: ${AYAR_DOSYA} yok; kayıt atlandı.`);
    console.log(`elle ekle: "plugin": ${JSON.stringify(KAYITLAR)}`);
    return;
  }

  const ham = await fs.readFile(AYAR_DOSYA, "utf8");
  const ayar = JSON.parse(ham);
  const dizi = Array.isArray(ayar.plugin) ? ayar.plugin : [];
  const eksik = KAYITLAR.filter((k) => !dizi.includes(k));

  if (eksik.length === 0) {
    console.log("kayit: zaten hepsi var");
    return;
  }

  await fs.writeFile(`${AYAR_DOSYA}.bak-kahin-kur`, ham, "utf8");
  ayar.plugin = [...dizi, ...eksik];
  await fs.writeFile(AYAR_DOSYA, `${JSON.stringify(ayar, null, 2)}\n`, "utf8");
  console.log(`kaydedildi ${eksik.join(", ")}`);
  console.log(`yedek: ${AYAR_DOSYA}.bak-kahin-kur`);
  console.log("opencode'u yeniden başlatınca kancalar yüklenir.");
}

main().catch((hata) => {
  console.error(`kurulum hatasi: ${hata?.message ?? hata}`);
  process.exitCode = 1;
});
