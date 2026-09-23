# GOD MODE — Yol Haritası

> Kural: tek build hakkı. Build'siz kanıt olmadan kaynak ağaca port yok, port olmadan build yok.
> "Bypass" yasaklı kelime — yapılan iş kaldırım/geçiş kartıdır, dolanma değil.
> Test hedefi her zaman GERÇEK site. Kukla sayfa yasak.

## Hedef
Camoufox 152.0.4-beta.30 tabanlı, otomasyon bağlamında güvenlik duvarına takılmayan tarayıcı:
cross-origin iframe + kapalı shadow root içinde Turnstile checkbox'a privileged erişim,
insan-sayılan tıklama, cf_clearance tutma. Uzun vade: duvar üreten kodun sistematik kaldırımı.

## Kök Neden (kanıtlı)
- `JugglerFrameChild` child frame'de erken dönüyor; FrameTree/Runtime sadece en üst frame'de kuruluyor.
- `Runtime.js` evaluate sandbox'ı `Cu.Sandbox([this.domWindow()])` — pencere yetkisi, sistem yetkisi değil.
- `windowUtils` ChromeOnly — cross-origin'den erişilemez (stok Gecko).
- Camoufox'un 5 ilgili yaması (shadow-root-bypass, chromeutil, debugger-invisible, trusted-automation, cross-process-storage) duvara dokunmuyor (Grok deep-research raporu).
- Baz: Firefox 152.0.4 release + 60 dosyalık Camoufox overlay (37 applied patch).

## Faz 0 — Build Garantisi (DERLEME YOK)
- [x] 0.1 Camoufox tarifini klonla, `make dir` ile yamalı kaynak ağacı kur → HAZIR (`build/camoufox/camoufox-152.0.4-beta.31/`, 5.6GB, `_READY` vuruldu, 0 yama hatası)
- [ ] 0.2 Bizim 2 dosyayı ağaca koy, `patch --dry-run` + `mach configure` kanıtı
- [ ] 0.3 mozconfig: `-j8`, düşük-RAM linker (yavaş ama patlamaz)

## Faz 1 — Build'siz Kanıt (omni.ja repack, GERÇEK siteler)
- [ ] 1.1 İki dosyayı kurulu binary'nin omni.ja'sına repack, `-purgecaches` + fresh profil
- [ ] 1.2 Site A (CF-only): 10 tekrar — checkbox + checked + cookie
- [ ] 1.3 Site B (Turnstile-only): 10 tekrar
- [ ] 1.4 Site C (ikisi bir arada): 10 tekrar
- [ ] 1.5 30/30 = dondur; altı = bırak + sebep yaz

## Faz 2 — Port + TEK Build
- [ ] 2.1 Kanıtlı dosyaları kaynak ağaca port et, diff'i dondur, onay al
- [ ] 2.2 TEK build (12 saat bandı, -j8, dokunulmaz)
- [ ] 2.3 Paket + sha256 + yedek; 3 sitede son doğrulama

## Faz 3 — God Mode Anatomisi (build sonrası)
- [ ] 3.1 Çalışan sistemden duvar haritası: hangi C++ kapısı ne zaman devrede
- [ ] 3.2 Sonraki hedefler bu haritadan seçilir (yine: build'siz kanıt → tek build)

## Siteler (void0x14 bulacak)
- A: CF-only → _
- B: Turnstile-only → _
- C: ikisi → _

## Log
- 2026-09-19: harita açıldı. Sırada 0.1.
- 2026-09-19: 0.1 başladı — tarif klonu bitti (1.5GB), `make dir` koşuyor (pid 104599, ulimit 2.5GB, nice 19). NOT: tarifte release=beta.31, bizim binary beta.30 — sürüm kayması 0.2'de ele alınacak. Sistem: 16 core, 15.6GB RAM (4.1GB boş), 147GB boş disk.
- 2026-09-19: 0.2 ROTADAN ÇIKTI. child-agents yaması ÖLDÜ — TargetRegistry.js:431 parent da child actor reddediyor, setActor tek channel (kanıtlı). Yeni rota: per-frame evaluate routing (mekanik hazır, wikipedia cross-origin kanıtı). WebGL düzeltildi: user.js Python-repr bug'ı (False/True) JSON'a çevrildi + LIBGL_ALWAYS_SOFTWARE=1 (mirage.py). GL-OK kanıtlı. CF bu IP'ye 403 veriyor (curl kanıtlı) — nopecha test edilemiyor, A/B/C siteleri bekleniyor. MCP server restart gerekli (kahin tool'ları düştü).
