# Kahin — Durum Modları: `ağırbaş` ve `keş`

> SAHİP KARARI. Bu belge bağlayıcıdır. Ajan bu belgede yazılı olmayan hiçbir
> "iyileştirme", "güvenlik sıkılaştırması" veya "izolasyon" varsayımı ekleyemez.
> Kullanıcı yerelde çalışır ve güvenlik umursamaz. Amaç pragmatik çalışan sistemdir.

## 1. Neden var

Kanıtlanan arıza: Bitwarden'a bir kez giriş yapıldı, ayarlar (Never timeout)
yapıldı, tarayıcı kapatıldı; sonraki açılışta Bitwarden **tekrar giriş istedi**.
Kök neden: kalıcı durum için **tek ve garantili bir ev yoktu**. Girişin yapıldığı
profil ile sonradan açılan profil aynı olmak zorunda değildi. Profil/uzantı
deposunda hesap yoktu; yalnızca global ayarlar vardı.

Bu belge, durumu iki net moda ayırır. İki mod **serbestçe** seçilir.

## 2. Sözleşme

### 2.1 `ağırbaş` — kalıcı

- **Tek ve sabit ev.** Her açılışta aynı dizin. Verilmezse daima aynı sabit yola
  düşer.
- **Hiçbir şey geçici değildir.** Tarayıcı kapanınca çerez, `localStorage`,
  IndexedDB, uzantılar ve uzantı durumu (Bitwarden girişi/ayarları) **silinmez**.
- **Bitwarden burada yaşar** (bkz. §3).
- Durum kaybı yaratan hiçbir işlem (profil silme, uzantı deposu temizleme,
  başlangıçta yeniden kurulum) ağırbaş yolunda yapılamaz.

### 2.2 `keş` — geçici, unut beni

- **Tamamen geçici.** Her açılışta **yeni ve benzersiz** dizin.
- **Anonim.** Her açılışta taze parmak izi/kimlik.
- **Kahin kapanınca yok olur.** `kahin_browser_stop` bu dizini ve içindeki her
  şeyi siler; bir daha erişilemez.
- Çökme/kill artıkları sonraki başlangıçta süpürülür.
- Bitwarden keş'te **yalnızca hesap otomasyonu dalında** taşınır (bkz. §2.4).

### 2.3 Serbest seçim

- Her mod, istenildiği gibi çalıştırılabilir. "Yalnızca ağırbaş" ya da
  "yalnızca keş" diye bir kural **yoktur**; seçim kullanıcınındır.
- `mode` açıkça verilmelidir (sessiz varsayılan yok). Verilmezse araç
  başlatmaz, iki modun sözleşmesini döndürür.
- `keş` yalnızca `ephemeral_ack=true` ile başlar (hiçbir şeyin kalıcı
  olmayacağının açık onayı).
- Eski `persistent_profile` bayrağı modlara eşlenir ama `mode` verilirse
  `mode` kazanır.

### 2.4 Otomasyon modu — ayrı eksen

Otomasyon modu ağırbaş/keş'ten **bağımsız, bambaşka bir moddur**. Durum
ömrüyle ilgisi yoktur.

**Hesap gerektiğinde her zaman Bitwarden kasasından alınır.** Sıra sabittir:

1. Sistemde o site için **passkey varsa** → direkt onunla girilir.
2. Passkey yoksa **credentials varsa** → onunla girilir.
3. **İkisi de yoksa** → kullanıcıya "Bitwarden'a ekle, gir" denir. Bu kadar.

Ağırbaş **her zaman** Bitwarden taşır. Keş, Bitwarden'ı yalnızca bu hesap
ihtiyacı doğduğunda taşır; bunun dışında keş'te Bitwarden'a ihtiyaç yoktur ve
yüklenmez. Ağırbaş/keş seçimi hesap ihtiyacını belirlemez; hesap gerektiğinde
mod ne olursa olsun Bitwarden devrededir.

**Roadmap:** 3. adımdaki kullanıcıya bırakılan aşama da otonomlaştırılacaktır —
hesabı da AI açar.

### 2.5 Dizin adları

Kanonik mod adları sahibinin verdiği Türkçe adlardır (`ağırbaş`, `keş`).
Dizin adları dosya yolu güvenliği için ASCII kalır (`agirbas/`, `kes/`);
ASCII yazımlar parametre olarak da kabul edilir. İngilizce karşılık uydurulmaz.

## 3. Bitwarden — gömülü ve pinli

Camoufox'a uBlock Origin nasıl gömülüyorsa, Bitwarden de Kahin'e gömülüdür:

- **Pinli kaynak.** XPI URL'si, SHA-256'sı, sürümü ve Gecko kimliği kodda
  sabittir; uyuşmazsa kurulum reddedilir.
- **Bir kez indir, bir kez çıkar.** `$KAHIN_HOME/addons/bitwarden/` altında
  tutulur; var olan doğrulanmış kopya **yeniden indirilmez/çıkarılmaz**.
- **Bir kez profile kur.** Ağırbaş profilinde `extensions/<gecko-id>.xpi`
  olarak kalıcı profil eklentisi bulunur. Kurulum idempotenttir; dosya
  yerindeyse kopyalanmaz.
- **Asla her başlangıçta sıfırdan yüklenmez.** Başlangıç yolu uzantıyı geçici
  (temporary) eklenti olarak yüklemez; bu depolama kimliğini böler ve girişi
  kaybettirir.
- **Tek giriş.** Kullanıcı yalnızca ilk kurulumda bir kez giriş yapar.
- **Varlık iddia edilmez, doğrulanır.** Başlangıç yanıtı uzantının profil
  kaydını (`extensions.json`) okur: `present_active`, `present_disabled`,
  `present_pending_first_run`, `missing` veya `unavailable`. "Kurdum" demek
  yetmez; silinmiş/pasif uzantı görünür olmalıdır.

## 4. Kilit ve zaman aşımı — kökten kapatma

Bitwarden **kilitlenmemeli** ve **zaman aşımına uğramamalıdır**. Tercih sırası:

1. **Kökten kapatma** (tercih edilen): uzantının kendi ayar deposuna yazılarak
   kilit/timeout davranışının tamamen devre dışı bırakılması; elle uğraş yok.
2. Kökten mümkün değilse uzantının kendi ayar arayüzünden otomatik kapatma ve
   doğrulama. Bu adım **otonomdur**: ilk girişten sonra ajan yapar, kullanıcıya
   soramaz.

Zorunlu ayarlar: Vault timeout = **Never**; tarayıcı yeniden başlatıldığında
kilitlenme **kapalı**. Ve bunların **yeniden başlatma sonrası hâlâ geçerli
olduğu doğrulanır**.

Bilinen mekanik (araştırma, kanıtlı): ayar `browser.storage.local` içinde
`user_<uid>_vaultTimeoutSettings_vaultTimeout = "never"` olarak durur; kökten
yazım için `policies.json` yetersizdir (managed_schema'da yok). "Never"
seçilince şifre çözme anahtarı diske yazılır ve yeniden başlatmada kasa
otomatik açılır — **ama Never set edildikten sonra bir kez unlock gerekir.**

### 4.1 Kök yazım — canlı kanıt durumu

Uygulama: `kahin/passkey_ui.py` → `configure_vault_timeout_root(client)`.
Bitwarden popup'ı gerçek bir uzantı sayfasıdır; ayar oradan uzantının kendi
depolama API'siyle yazılır, arayüz tıklaması yoktur. Değerler Bitwarden'ın
kendi sarmalayıcısıyla (`{"__json__": true, "value": JSON.stringify(v)}`)
yazılır ve **geri okunup doğrulanır**; uyuşmazsa `failed` döner.

Canlı ölçülen (giriş gerekmeden, gerçek Camoufox + gerçek Bitwarden uzantısı):

- Marionette sandbox'ında `window.browser` ve `window.chrome` **görünmez**
  (Xray gizler) — `typeof` `"undefined"` döner.
- `window.wrappedJSObject.browser` **görünür ve gerçek API'dir**;
  `browser.storage.local` erişilebilir. Okuma çalıştı (`readOk: true`).
- Hesap yokken okuma `{}` döner; kök ayar yolu doğru şekilde
  `login_required` verir (yanlış negatif değil).
- Erişilemeyen depo asla "hesap yok" sayılmaz: nesne dönmeyen okuma
  `unavailable` olur (sessiz `{}` sahtekârlığı engellendi).

**Canlı doğrulandı (gerçek giriş + yeniden başlatma, 2026-09-26):**

```
timeout: {"vault_timeout": "never", "vault_timeout_action": "lock"}, never: true
auto keys: ["31fe465e-..._user_auto"]
POPUP HASH: #/tabs/vault      <- kasa AÇIK, kilit ekranı değil
LOCKED?: False                <- şifre alanı yok
```

Yani yeniden başlatmadan sonra Bitwarden **kendiliğinden açılıyor**; giriş yok,
master password yok.

**Kritik mekanik (kanıtlanan tuzak):** `never` tek başına yetmez. Bitwarden
auto-unlock anahtarını (`<uid>_user_auto`) **yalnızca kasa açılırken** yazar.
Giriş `onRestart` varken yapıldıysa anahtar yazılmaz ve yeniden başlatmada kasa
`#/lock` olur — sahibin daha önce yaşadığı tam olarak buydu. Doğru sıra:

1. Kullanıcı bir kez giriş yapar.
2. Kod `never` + `lock` ayarını kökten yazar (`configure_vault_timeout_root`).
3. `never` devredeyken kasa **bir kez** açılır → `<uid>_user_auto` yazılır.
4. Bundan sonra kalıcı: her açılışta kasa açık.

Bu iki adım `scripts/bitwarden_ilk_kurulum.py` ve `scripts/bitwarden_kasa_ac.py`
ile otonom yürütülür; kullanıcıdan yalnızca giriş/açma anında tek bir elle
işlem istenir, gerisi kod.

## 5. Ajan yüzey minimizasyonu (en sancılı nokta)

Ajanlar gerizekalıdır; eğitim setlerinde yalnızca Playwright/CDP gibi birkaç
şey vardır. Bu yüzden:

- **Kritik her şeyi kod halleder.** Ajanın hatırlaması gereken bayrak, sıra,
  seçim bırakılmaz. Mümkün olduğunda her şey kodda çözülür.
- **Ajan minimum yüzey alanı görür.** Ajan tanıdık bir komut verir; Kahin onu
  arkada doğru yere yönlendirir. İki taraf da mutlu, durum kaybolmaz, ajanın
  farkındalık sorunu yaşanmaz.
- **Seçim ajanına bırakılmaz.** Kimlik bilgisi/passkey seçimi gibi işler kod
  tarafından yapılır; ajan bırakılınca azar ve sapıtır.
- **Ajan yalnızca son çaredir.** Kodun halledemediği, karıştıracağı veya
  insanımsı anlarda ajan devreye girer; ajan kendini gizlice salar.
- Bu sayede statik "durum makinesi" yükünden de kurtulunur.

Mod seçimi de bu ilkeye tabidir: kod, görevin gerektirdiği modu mümkün olduğunca
kendisi belirler; ajanı yalnızca gerçekten belirsiz/insanımsı anda darlar.

### 5.1 Mod koruması (hook)

Ajanın **yanlış modu** seçmesini iki hook engeller: OpenCode eklentisi
(`~/.config/opencode/plugins/kahin-mode-guard.js`) ve Claude Code `PreToolUse`
hook'u (`~/.claude/hooks/kahin-mode-guard.sh`). İkisi de aynı bağımsız çekirdeği
kullanır (`kahin-mode-guard-core.mjs`); kural tek yerde durur. Kahin tarafındaki
`mode` şeması **değişmez** — koruma yalnızca ajan katmanıdır.

Kurallar (`resolveGuard`), sırayla:

- **R0** — Araç Kahin başlatma aracı değilse (`browser_start` içermiyorsa): izin.
- **R1** — `mode` verilmemişse: izin. Kahin `mode_required` döndürür; bu zaten
  sözleşmeli "sor"dur. Koruma `mode`'u dayatmaz.
- **R2 (argüman-kanıtı)** — `passkey_mode=true` **veya** `profile_dir` dolu
  **veya** `addons` boş değilse doğru mod `ağırbaş`'tır. `mode='keş'` ise
  **engellenir**.
- **R3** — Argümanlar arasında çelişki yoksa: izin.

Koruma **yalnızca çağrının kendi argümanlarına** bakar. Görev metni okunmaz,
kelime tahmini yapılmaz — sahip bu yaklaşımı reddetti.

Engelleme, doğru modu **ve** kanıt tokenını taşır ve ajanı doğru modla
**yeniden çağırmaya** zorlar. Koruma modu sessizce **yeniden yazmaz**
(`output.args`'a dokunulmaz). Hata veya eksik dosya kararı etkilemez: koruma
**açık uçlu (fail open)** başarısız olur. Her karar
`~/.config/opencode/plugins/kahin-mode-guard.log` dosyasına yazılır.

**Dosya yerleşimi.** Canlı kopya global eklenti dizinindedir
(`~/.config/opencode/plugins/kahin-mode-guard.js` + `-core.mjs`) ve
`~/.config/opencode/opencode.json` içindeki açık `plugin` dizisinde listelidir.
Sürüm kontrolündeki kaynak kopya `scripts/opencode-plugin/` altındadır; repo
`.opencode/plugins/` dizininde kopya tutulmaz.

### 5.2 Mod koruması — canlı kanıt

OpenCode yeniden başlatıldıktan sonra gerçek MCP araç çağrılarıyla ölçüldü
(2026-09-26). `tool.execute.before` **MCP araçları için de tetikleniyor**:

| Çağrı | Karar | Kural |
|---|---|---|
| `mode` verilmedi | izin → Kahin `mode_required` döndü | R1 |
| `mode="ağırbaş"` | izin → tarayıcı başladı (`state_mode=ağırbaş`) | R3 |
| `passkey_mode=true` + `mode="keş"` | engellendi | R2 |

Kararlar `~/.config/opencode/plugins/kahin-mode-guard.log` içinde durur.


### 5.3 Denetim (bağımsız, çürütmeye çalışan)

Mod koruması, durum modları, Bitwarden kurulumu/giriş kalıcılığı ve giriş
zinciri bağımsız denetçilerle yeniden ölçüldü. Bulunan ve **düzeltilen** gerçek
hatalar:

- **`AGIRBAS` normalleşmiyordu.** Türkçe katlama ASCII `I`'yı noktasız `ı`'ya
  çevirdiği için `"AGIRBAS"` → `"agırbas"` oluyor ve alias'ı ıskalıyordu; mod
  "bilinmeyen" sayılıp **fail-open** geçiyordu. Artık iki katlama da deneniyor
  (`fold` + `foldAscii`); canlı doğrulandı: `AGIRBAS → ağırbaş`, `KES → keş`.
- **`resolve_login` non-string girdide istisna atıyordu** (`urlsplit(123)`).
  Artık tip/boşluk koruması var → `unavailable / invalid_site_url`.
- **Boş `site_url` her sekmeye eşleşiyordu** (`indexOf("") === 0`) ve rastgele
  bir sekme için karar döndürüyordu. Artık reddediliyor.

- **Kelime kuralı kaldırıldı.** Koruma önce görev metnine bakıyordu; sahip
  bunu reddetti ("kelimeye bakarak iş yapıyorsun"). Artık yalnızca çağrının
  kendi argümanlarına bakıyor. Test: 12/12.

Koruma yalnızca **belirleyici çelişkide** ateş eder; sahibin onaylamadığı yeni
bir kural üretmez.

## 6. Ev (home) yerleşimi

```
$KAHIN_HOME (varsayılan: ~/.local/share/kahin)
├── agirbas/
│   └── profile/           # kalıcı, asla silinmez
├── addons/
│   └── bitwarden/         # pinli, çıkarılmış, bir kez (paylaşılan kaynak)
├── kes/                   # geçici çalışma kökü; stop'ta silinir
└── (eski) profile/        # agirbas/profile'e tek seferlik taşınır
```

Eski `profile/` dizini varsa ve `agirbas/profile` yoksa, tek seferlik atomik
taşıma yapılır; kullanıcının mevcut uBlock/Bitwarden verisi korunur.

## 7. Adlandırma politikası

- Sahibin verdiği Türkçe adlar korunur; İngilizce'ye **çevrilmez**.
  `kahin` → `oracle` gibi çeviriler yanlıştır. "Kahin" bir isimdir.
- Yeni kod, mevcut İngilizce adları çoğaltmaz; kullanıcıya/ajana görünen
  yüzeyde Türkçe tercih edilir.
- İngilizce kelime oyunu/şaka yapılmaz.

## 8. Not: "profil" çerçevesi

Sahip "profil" kelimesini Chrome profilleriyle ilişkilendiriyor ve bu
soyutlamayı gereksiz buluyor. Uzun vade istenen: gerçek tarayıcı profillerini
(ör. Chrome) Kahin'e **native import/export** etmek, kısmi seçip taşımak.
Bu, mevcut iki modun ötesinde ayrı bir yetenektir; şimdilik yalnızca nottur.

## 9. Teslimat durumu

1. **Mod çekirdeği** — `browser_start(mode=...)`, profil politikası, stop'ta
   keş temizliği, özetlerde mod bilgisi. **Bitti** (Faz 1).
2. **Gömülü Bitwarden** — pinli, idempotent, tek kurulum, doğrulanmış durum.
   **Bitti** (Faz 1).
3. **Kilit/timeout kökten kapatma** — ayar yazımı + yeniden başlatma
   doğrulaması. **Sırada** (Faz 2).
4. **Otomasyon modu** — ayrı eksen; hesap havuzu + kimlik/passkey doldurma
   kodu. **Sırada** (Faz 2/3).
5. **Passkey** — Camoufox Firefox 152'nin yerel Marionette sanal
   authenticator'ı (`WebAuthn:AddVirtualAuthenticator` / `AddCredential`)
   üzerinden; Bitwarden `keyValue` → Marionette `privateKey`. **Sırada**
   (Faz 3).

## 10. Kabul kapıları

- `keş` başlat → kapat → dizin yok. İkinci kez erişilemez.
- `ağırbaş` başlat → çerez/localStorage yaz → kapat → yeniden başlat → veri durur.
- Bitwarden: ilk kurulumda bir kez kurulur; ikinci başlatmada **indirme/çıkarma/
  kopyalama yapılmaz** (kanıt: dosya mtime değişmez, ağ isteği yok).
- Bitwarden durumu **profil kaydından** doğrulanır (silinmiş/pasif görünür).
- Bitwarden: giriş + Never + kilit-kapalı ayarları, **yeniden başlatma sonrası**
  hâlâ geçerli.
- `mode` verilmeden `browser_start` çağrılırsa tarayıcı açılmaz.
- `keş` `ephemeral_ack` olmadan açılmaz.

## 11. Açık maddeler (kaybolmasın)

### 11.1 Yapılacak işler

1. **Mod zorlamasının ikinci yarısı.** Şu an olan: yanlış modda `kahin_browser_start`
   çağrısı argüman çelişkisi taşıyorsa engellenir; ajan "durma" kancasıyla
   durdurulamaz. Eksik olan, sahibin *"şartlarımı sağlamıyorsan ilerleyemezsin"*
   dediği kısım: `tool.execute.before` ile her araç çağrısını reddedip ajanı
   şartı sağlamaya zorlayan kanca. **Şartı sahibi belirleyecek; uydurulmayacak.**
2. **`select_passkey` canlı denemesi.** Kasa passkey'i olan bir sitede FIDO2
   popout satır tıklaması canlı denenmedi (yalnızca birim testli).
3. **`fill_credentials` alan doldurma kanıtı.** Mesajın content script'e teslimi
   canlı kanıtlı (`triggered`); login formu olan bir sayfada alanların gerçekten
   dolduğu canlı denenmedi.
4. **Bitwarden'ın kendisinden okunacak passkey sayısı** per-origin eşlenemiyor:
   `rpId` şifreli. Tespit, kasa arayüzündeki detay görünümünden yapılıyor.

### 11.2 Bilinen sınırlar (dış koşullar)

5. **"Ömür boyu tek giriş" garanti edilemez.** `never` + `auto-unlock anahtarı`
   yerelde kalıcıdır; ancak sunucu tarafında oturumun geri alınması, hesap
   değişikliği veya kuruluş politikası gibi dış koşullar yeniden kimlik
   doğrulama gerektirebilir. Bu durumda Kahin bunu açıkça raporlar.
6. **opencode'un plugin API'sinde tur-bitişini engelleyen kanca YOK.** Bu yüzden
   "durma" kancası `session.idle` olayından sonra aynı oturuma devam mesajı
   gönderir; tur biter, oturum devam eder. Gerçek "tur bitmesin" davranışı
   opencode çekirdeğinde değişiklik gerektirir (kaynak:
   `packages/core/src/session/runner/llm.ts`, `run()` iç döngüsü).
7. **Plugin'ler açılışta yüklenir.** `kur.mjs` çalıştırıldıktan sonra opencode
   bir kez yeniden başlatılmalıdır.
8. **`chmod 444` durum dosyasını kilitlemez** (yazıcı temp+rename kullanıyor).
   Gerçek dokunulmazlık için `chattr +i` veya salt-okunur dizin gerekir.
9. **Mod koruması çağrı başına iki kez ateşliyor** — opencode config'i iki kez
   yüklüyor. Engelleme ilk atışta kesildiği için davranış doğru; gürlük var.
10. **Bitwarden girişi makine başına bir kez.** Hesap sahibin olduğu için
    taşınamaz; diğer her şey (profil, eklenti, ayarlar) kendiliğinden kurulur.

### 11.3 Önceden var olan kusurlar (bu iş kapsamında değil)

11. `mirage_dom_action` click, satır içi `onclick`'i tetiklemiyor.
12. `mirage_route` abort/fulfill eşleşmiyor.
13. `test_shared_screenshot...` bayat: ürün base64 değil dosya yolu döndürüyor.
14. `~/.config/opencode/opencode.json` varsayılan modeli bu opencode sürümünde
    bulunmuyor (`deepseek/deepseek-v4-flash-vision-exp`). Sahibin kararı;
    dokunulmadı.
