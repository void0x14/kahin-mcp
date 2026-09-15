# Changelog

## Unreleased

- feat(cf): rewrite `kahin_cf_clear` around the TR trust contract: frame-anchored native press/release, FakeShadowRoot-style shadow walk, 5 attempts with jittered 3s retries, and title-gate plus host-scoped `cf_clearance` evidence
- feat(watch): add localhost-only `kahin_mirage_watch_start` / `kahin_mirage_watch_stop` MJPEG live watch fed by the existing screencast pump; manual frame calls may race the watch pump
- verify(live): real nopecha Cloudflare attempt returned `cleared:false`, `method:timeout`, `clicks:0`; live watch `/snapshot.jpg` returned JPEG `ffd8ff`, and three screencast frames were ACKed with pending `0`

## [0.3.10] — 2026-08-09

- fix(harness): align lifecycle tests and screenshot measure with grace-window runtime
- fix(stealth): disable Camoufox humanize mouse trajectory (drops acks, wedges input)
- fix(release): retry npm pack verification after publish
- chore: prepare v0.3.9
- chore: refresh packaged kahin wheel
- fix: harden single-engine crawler runtime
- fix: preserve crawler rotation state
- feat: add single-engine crawler rotation
- docs: define crawler identity rotation contract
- fix: harden Kahin browser runtime
- update
- ci(release): tolerate npm registry propagation

## [0.3.9] — 2026-08-09

### Eklenen
- `kahin_crawl_start` / `kahin_crawl_status` / `kahin_crawl_results` /
  `kahin_crawl_pause` / `kahin_crawl_resume` / `kahin_crawl_stop` eklendi:
  tek aktif Camoufox/Mirage motoru ve tek mevcut sekme üzerinde çalışan,
  bounded kuyruk/sonuç günlüğü ve cursor destekli crawler yüzeyi.
- Crawler; sayfa, derinlik, süre ve gecikme bütçelerini sınırlar, `Retry-After`
  ile rate-limit durumunda bounded backoff uygular ve otomatik identity
  rotasyonunu aynı browser/sekme yaşam döngüsü içinde yürütür.

### Düzeltilen
- Aktif motor yeniden kullanılmadan tekrar browser açılması, degraded health
  durumunda canlı context'in gereksiz değiştirilmesi ve crawler sırasında
  sekmenin kaybolması engellendi.
- Sidecar stdin yazımları ve sayfa işlemleri bounded hale getirildi; engine
  çökmesi sonrası URL yeniden kuyruğa alınarak aynı crawler işi kontrollü
  recovery ile sürdürülebilir hale geldi.
- Humanized mouse trajectory artık Juggler'ın tamamlamadığı aynı-koordinat
  no-op dispatch'ini göndermiyor; gerçek hareket adımı korunuyor.
- CAPTCHA ve erişim engeli bypass edilmiyor: crawler challenge'ı açıkça
  `paused` durumuna geçiriyor ve ajan müdahalesi için aynı işi koruyor.
- Her rotation gerçek per-launch fingerprint değişimiyle doğrulanıyor;
  proxy/geo ayarları ve kayıtlı identity kullanımı yeniden başlatma/rotasyon
  sonrasında korunuyor.
- Proxy ile başlatma için Camoufox'un `geoip` extra'sı paket bağımlılığına
  alındı; temiz kurulumlar artık native proxy/geo ayarını eksik modül yüzünden
  başarısız bırakmıyor. GeoIP endpoint'i erişilemezse proxy korunarak
  proxy-only launch fallback'i uygulanıyor.
- npm launcher wheel install marker'ı ve Camoufox hazırlığı bounded komut
  süreleriyle tekrar kurulumları güvenli ve hızlı hale getiriyor.

### Gerçek doğrulama
- Paketlenmiş stdio MCP ile 146 tool ve 2 resource list/read doğrulandı.
- Gerçek crawler akışında tek engine PID ve tek sekme ile 4 sayfa başarıyla
  işlendi, 4 rotation gerçekleşti; rate-limit ve CAPTCHA challenge durumları
  tekrar çağrılarda tutarlı biçimde korundu.
- Gerçek engine kill sonrası crawler 1 recovery attempt ile 5 başarılı sayfayı
  tamamladı; ayrı identity probe'unda her rotation fingerprint hash'i değişti.
- Dış web sayfasında stealth audit `14/14`, DOM snapshot/event reset-dropped,
  accessibility tree ve `browser_stop` sonrası parent MCP transport bağlantısı
  doğrulandı.

## [0.3.8] — 2026-08-08

### Eklenen
- `kahin_mirage_expect` / `kahin_mirage_check` / `kahin_mirage_uncheck` /
  `kahin_mirage_select_option` / `kahin_mirage_dblclick` / `kahin_mirage_drag` /
  `kahin_mirage_wait_for_text` / `kahin_mirage_wait_for_timeout` /
  `kahin_mirage_route` eklendi: retry'li web-first assertion, doğrulamalı
  checkbox/radio/select eylemleri, gerçek çift tıklama, sınırlı drag-and-drop,
  metin/süre bekleme ve tek sonraki istek için abort/continue/fulfill route.
- `kahin_mirage_wait_selector` artık `state=attached|visible|enabled`
  (varsayılan `visible`) destekler; timeout yanıtı `code:"timeout"` ve son
  `reason` ile döner.
- `kahin_mirage_query` / `kahin_mirage_query_all` / `kahin_mirage_type` /
  `kahin_mirage_wait_selector` locator motorunu kabul eder (`css=` / `text=` /
  `role=` / `xpath=` / `nth=` ve `>>` zincirleme).
- `kahin_mirage_click` / `kahin_mirage_hover` / `kahin_mirage_focus` aksiyon
  öncesi Playwright tarzı actionability bekler (görünür, enabled, stabil,
  engelsiz; retry + `timeout` parametresi).
- `kahin_navigate` artık `wait_until=commit|domcontentloaded|load|networkidle`,
  bounded `timeout` ve `referer` kabul eder; lifecycle beklemesi aşılırsa
  `code:"navigation_timeout"` döner.
- Faz 2 agent-native yüzey: `kahin_mirage_snapshot` (ref'li, token bütçeli
  satırlar; truncation asla sessiz değildir), `kahin_mirage_fill_form`
  (ref'lerle çok alanlı doldurma; stale ref `requiresSnapshot` döner) ve
  click/type/check/uncheck/select_option/dblclick/drag'e opsiyonel
  `return_snapshot` kancası.
- Oturum kalıcılığı: `kahin_mirage_state_save` / `kahin_mirage_state_load`
  (url + cookie + local/sessionStorage; mutlak yol zorunlu, load önce
  kaydedilen url'e döner).
- Kimlik kalıcılığı: `kahin_identity_new` / `kahin_identity_save` /
  `kahin_identity_list` / `kahin_identity_delete` / `kahin_identity_report` +
  `kahin_browser_start(identity=...)` ile gerçek Camoufox fingerprint
  pinleme; rapor canlı `navigator.userAgent`'ı sayfa-içi doğrular.
- `kahin_agent_status`: agent döngüsü özeti — engine liveness, sayfa durumu,
  sekme sayısı, gerçek DOM-stream bookkeeping'inden `refsLive`/`domCursor`,
  dialog/network/console sayaçları ve identity; engine yoksa yapılandırılmış
  idle yanıt, asla hata fırlatmaz.
- Faz 3 stealth/anti-detect yüzeyi: `kahin_stealth_audit` (14 salt-okunur
  leak probe'u, skor `{passed, total, ratio}`; hiçbir check atlanmaz),
  `kahin_mirage_mouse_trajectory` / `kahin_mirage_click_humanized` ve
  cadence'li `kahin_mirage_key_text` (jitter'lı Bézier yörünge, gerçek DOM
  tıklaması, jitter'lı tuş hızı; hepsi seed'li deterministik).
- Domain başına kimlik rotasyonu: `kahin_identity_pin` / `kahin_identity_unpin`
  / `kahin_identity_pins` / `kahin_identity_for_domain` — kanonik domain
  anahtarlı, bounded ve doğrulanmış `~/.config/kahin/pins.json` deposu.
- `kahin_fingerprint_report` (canlı sayfadan sitenin göreceği fingerprint;
  emülasyon onayından kanıt üretmez) ve `kahin_proxy_resolve` + `kahin_browser_start(proxy=...)`
  (proxy exit-IP geo + timezone/locale/geolocation önerileri; URL kimlik
  bilgileri asla loglanmaz; aktif engine config'iyle çakışma
  `engine_config_conflict`).
- Stealth CI kapısı: `KAHIN_REQUIRE_STEALTH=1` altında audit ratio ≥ 0.8 ve
  identity rotasyonu tam fingerprint özetini değiştirmek zorunludur;
  `scripts/stealth-regression.py` drift-watcher'ı sabitlenmiş
  `camoufox-harness/tests/perf/stealth-baseline.json` ile karşılaştırır
  (0 = temiz, 1 = yeni leak, 2 = ortam; baseline yalnızca
  `KAHIN_UPDATE_BASELINE=1` ile yeniden yazılır) ve GitLab `stealth-regression`
  job'ı (`stage: verify`, real-e2e ile aynı unprivileged/GTK kurulumu)
  schedule/main'da suite + regression'ı çalıştırır.
- Faz 4 performans yüzeyi: Zig sidecar non-blocking'e geçti — tek-in-flight
  darboğazı kalktı, stdin işleme browser yanıtını asla bloklamaz ve birden
  fazla istek aynı anda in-flight olabilir (IPC sözleşmesi değişmedi). N=20
  `Runtime.evaluate` concurrency probe'u
  (`camoufox-harness/tests/perf/concurrency.md`): seri 74.78 → 28.56 ms,
  paralel duvar süresi 56.89 → 8.71 ms (6.5× düşüş), ratio 1.314 → 3.281.
- `kahin_engine_stats` eklendi: monotonic clock ile uptime + healer
  tracker'dan bounded per-tool rollup (`tool_calls`/`tool_errors`,
  `top_slow` ≤ 10, `last_error`); tüm `safe()` süre ölçümleri
  `time.monotonic()`'a taşındı; engine yoksa yapılandırılmış
  `engine_unavailable` yanıtı döner.
- Identity başına bounded profile prewarm: stabil sha256 identity hash +
  ölçülen hazırlık süreleri in-process cache (max 8, FIFO) ve
  `~/.cache/kahin/profiles/<hash>.json` içinde tutulur; gerçek launch işi
  asla atlanmaz (dürüst metadata/reuse kaydı; kimlik payload'ı metadata'da
  saklanmaz).

### Düzeltilen
- Obscura/Shadow doğrudan screenshot çağrısını artık fail-closed reddeder;
  görsel capture sözleşmesi Mirage’a ait kalır ve generic CDP yolu Mirage’a
  doğru şekilde yükseltilir.
- Stealth CI gate’i Xvfb ile sabit bir grafik yüzeyinde ve `TZ=Etc/UTC` ile
  çalışır; timezone probe’u geçerli `UTC` çıktısını da kabul eder. Böylece
  runner’ın DISPLAY/TZ farkları gerçek stealth drift’i gibi raporlanmaz.
- Release tag’leri stealth regression kapısından geçmeden npm publish’e
  ulaşamaz; npm publish sonrası registry’den sürüm tekrar okunarak local
  package sürümüyle birebir eşleşme kanıtlanır.
- Identity rotation e2e gate’i Linux/Windows kimlik çiftiyle en az iki canlı
  fingerprint boyutunu deterministik doğrular; yalnızca runner WebGL’ine
  bağlı rastgele identity çakışması artık release’i düşürmez.
- CI browser image’larına Mesa/llvmpipe runtime’ı eklenerek WebGL stealth
  probe’u gerçek software-rendered context üzerinde doğrulanır; baseline
  gevşetilmez.
- Runner’da Mesa’nın gerçek software renderer’ı açıkça `llvmpipe` olarak
  seçilir; WebGL gate’i yalnızca kütüphanelerin kurulu olmasına güvenmez.
- Stealth audit’in webgl check’i artık runner grafik yığınından bağımsız:
  WebGL API varlığını (context entry point) doğrular; GL’siz CI runner’ında
  context oluşturma başarısızlığı artık sahte “leak” olarak raporlanmaz.
  CI browser gate’leri ayrıca Mesa GL kütüphanelerini kurar, böylece GL
  sunan runner’larda `fingerprint_report` gerçek WebGL değerlerini okur.
- `lib/` içindeki gömülü wheel HEAD kaynağından yeniden üretildi: Obscura
  screenshot guard’ı, stealth/agent/reliability modülleri ve güncel Zig
  sidecar dahil — npm paketinin taşıdığı wheel ile kaynak ağacı senkron
  kalır.

## [0.3.7] — 2026-08-06

### Eklenen
- Gerçek Camoufox/Mirage akışı için canlı DOM snapshot/event cursor sözleşmesi;
  stale node, reset ve dropped durumları artık ajanı yeni snapshot almaya zorlar.
- CI üzerinde Playwright veya sahte e2e yerine checkout'tan derlenen Zig sidecar,
  resmi Camoufox ve zorunlu gerçek browser testlerinden oluşan release gate'i.

### Düzeltilen
- Mirage çağrıları, DOM/input/network/storage/screencast ve iframe işlemlerinde
  doğru page/session sahipliğine sabitlendi; paralel sekmeler birbirine karışmaz.
- Sidecar IPC ve process lifecycle hataları artık yutulmadan fail-closed davranır;
  screenshot, upload, accessibility ve network yanıtları bounded hale getirildi.
- npm launcher her kurulumda gömülü wheel'i günceller ve Camoufox binary'sini
  hazırlar; npm release sorgusu ağ/auth hatalarını yanlışlıkla başarı saymaz.
- GitLab gerçek-browser job'u Firefox'u Docker root olarak çalıştırmaz; npm
  publish artık release-check yanında real-e2e kapısına da bağlıdır.
- CI image'ına Camoufox'un GTK/DBus/audio/X11 runtime bağımlılıkları eklendi;
  testten önce gerçek binary `--version` ile başlatılabilirliği doğrulanıyor.
- Network response-body çağrıları, request event'i gövde erişilebilir olmadan
  geldğinde native completion yarışını sınırlı retry ile güvenle tamamlıyor.

### Testler
- Gerçek Camoufox/Mirage uygulama suite'i: 120 passed.
- Zig sidecar testleri: 201/201 passed; ruff, compile, shell/YAML ve npm pack
  release kontrolleri başarılı.

## [0.3.6] — 2026-08-06

### Düzeltilen
- `Page.getLayoutMetrics` ve `Page.stopLoading` CDP çağrıları Mirage içinde
  gerçek sayfa ölçümü/evaluate ile karşılanıyor; Juggler'ın olmayan methodları
  artık yanıltıcı typo yerine `unsupported_on_engine` olarak raporlanıyor.
- Shadow -> Mirage handoff event/network buffer'larını silmiyor; görsel
  capability'ye geçmeden önce toplanan gerçek teşhis verisi korunuyor.
- npm release script'i sürüm registry'de zaten varsa CI auth olmadan idempotent
  şekilde başarılı tamamlanıyor.

### Testler
- Gerçek Camoufox'ta CDP layout metrics, Shadow'dan mobile viewport ve
  screenshot handoff doğrulandı.

## [0.3.5] — 2026-08-06

### Düzeltilen
- `kahin_browser_start()` artık varsayılan olarak gerçek Camoufox/Mirage
  başlatıyor; screenshot, mobile viewport, screencast, upload ve accessibility
  çağrıları Shadow seçilmiş olsa bile Kahin içinde görsel backend'e yükseltiliyor.
- Yeni capability sözleşmesi `kahin_engine_health` yanıtında motorun gerçek
  görsel/mobile/DOM-stream/a11y/screencast sınırlarını raporluyor; generic
  `kahin_screenshot` ve `Page.captureScreenshot` yolu dış otomasyon fallback'i
  kullanmadan Camoufox'a geçiyor.
- Shadow/Obscura artık bağımsız MCP/OpenCode süreçleri arasında sabit `9241`
  portunu paylaşmıyor; varsayılan başlatma her child için geçici loopback portu
  kullanıyor ve gerçek portu raporluyor.
- Port çakışmasında ikinci child'ın yabancı browser WebSocket'ine bağlanıp
  yanlışlıkla `started` dönmesi engellendi; child kararlılığı doğrulanmadan
  engine state'e yayınlanmıyor ve başarısız child temizleniyor.
- Mirage/Zig sidecar shutdown cancellation path'i child ve IPC kaynaklarını
  güvenle temizliyor.

### Testler
- İki bağımsız Shadow instance'ı ve explicit port çakışması gerçek Obscura
  binary'siyle regresyon testine alındı.
- Shadow -> Camoufox screenshot handoff ve varsayılan motor gerçek browser ile
  doğrulandı.
- Python runtime suite: 115 passed; Zig sidecar tests/build: passed.

## [0.3.4] — 2026-08-05

### Eklenen
- Mirage/Juggler için gerçek-zamanlı DOM stream: bounded semantic snapshot,
  cursor tabanlı MutationObserver + input/focus/click/change event delta'ları,
  reset/dropped sinyalleri ve canlı nodeId action yüzeyi.
- `kahin_mirage_dom_start`, `kahin_mirage_dom_snapshot`,
  `kahin_mirage_dom_events`, `kahin_mirage_dom_action`,
  `kahin_mirage_dom_stop` olmak üzere 5 yeni tool.
- Ajanların source okumadan kullanacağı [AI-native Juggler kılavuzu](docs/juggler-ai-native.md).

### Değişen
- MCP server instructions artık adaptive Mirage DOM akışını, cursor/reset ve
  canlı nodeId kurallarını doğrudan ajana bildiriyor.

### Testler
- Gerçek Camoufox e2e: DOM snapshot + delta + canlı type/click action ve
  navigation sonrası stream reset regresyonları.

Tüm önemli değişiklikler bu dosyada tutulur. Format: [Keep a Changelog](https://keepachangelog.com/tr/1.1.0/) — [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.3.3] — 2026-08-05

### Düzeltilen
- Firefox/Camoufox child süreci öldüğünde stale engine referansı erken silinmiyor; `kahin_browser_stop` artık sidecar'ı güvenle reap edip temizleyebiliyor.
- Ölü engine health/tool hataları açıkça `kahin_browser_stop` ardından `kahin_browser_start` akışını öneriyor.

### Testler
- Gerçek Firefox child kill → health dead → stop → yeniden start regresyonu doğrulandı.
- Phantom liveness testleri: 13 passed.

## [0.3.2] — 2026-08-05

### Düzeltilen
- Camoufox/Mirage artık aynı browser sürecini ve sekmeleri yeniden kullanıyor; tekrar eden `browser_start` çağrıları ikinci browser açmıyor.
- Başlangıç, sağlık kontrolü ve stop timeout'larında sidecar/Camoufox orphan süreçleri temizleniyor.
- Mirage araçlarının ilk sekme yokken timeout olması düzeltildi; eşzamanlı ilk çağrılar tek sekmeye birleşiyor.
- CDP çağrıları Target/Input/Emulation/Network eşdeğerlerine otomatik yönlendiriliyor ve CDP biçimli sonuç döndürüyor.
- Eşzamanlı Juggler çağrılarında yanlış pending request silinmesi düzeltildi.

### Testler
- Python uygulama testleri: 106 passed
- Zig sidecar testleri: passed

## [0.3.0] — 2026-08-03

### Eklenen
- **7 yeni tool (toplam 104)**:
  - **Iframe erişimi**: 12 DOM tool'una opsiyonel `frame_id` parametresi — `Runtime.callFunction` ile hedef frame'in `executionContextId`'si üstünde evaluate; `Runtime.executionContextCreated/Destroyed/Cleared` event'lerinden frame→context map (Mirage reader loop'ta)
  - **Dosya yükleme (2)**: `kahin_mirage_set_file_chooser_intercept`, `kahin_mirage_upload_files` — `Page.setInterceptFileChooserDialog` + `fileChooserOpened` bekleme + `Page.setFileInputFiles` (absolute path zorunlu, multi-file, timeout)
  - **Canlı screencast (4)**: `kahin_mirage_screencast_start/frame/stop/pending` — `Page.startScreencast` (base64-JPEG frame'ler), her frame için zorunlu `screencastFrameAck` (kMaxFramesInFlight=1), FIFO frame queue, stop'ta temizlik
  - **Erişilebilirlik (1)**: `kahin_mirage_accessibility_tree` — `Accessibility.getFullAXTree` (Camoufox-only; upstream Playwright'da yok, CDP'de de yok)
- **Gerçek HTTP e2e** (`tests/test_e2e_network.py`): network_requests, get_response_body, interception continue/abort — localhost stdlib server ile, dış ağ yok
- **Iframe e2e** (`tests/test_e2e_iframe.py`): frame tree, iframe içi query/click/type — 4 PASS
- **Upload e2e** (`tests/test_e2e_upload.py`): single/multi dosya byte-exact, timeout, absolute path zorunluluğu — 4 PASS
- **Screencast e2e** (`tests/test_e2e_screencast.py`): start/clamp, JPEG magic + ack stream, pending queue, stop temizliği — 4 PASS
- **A11y e2e** (`tests/test_e2e_accessibility.py`): role/name/placeholder yansıması, nodeCount/truncated — 4 PASS

### Değişen
- Sidecar router: `Accessibility.*` page session kuralına eklendi (önceden root'a düşüyordu → `-32000 "Handler for ... does not implement"`); 196 Zig test
- `Network.enable`'in Juggler'da OLMADIĞI doğrulandı (event'ler default) — network tool'ları gerçek HTTP'te as-is çalışıyor, bug yok
- data: URL network skip testi kaldırıldı (yerini gerçek HTTP e2e aldı)

### Düzeltilen
- `Accessibility.getFullAXTree` route bug'ı (page session'a yönlendirme)

## [0.2.0] — 2026-08-03

### Eklenen
- **65 yeni Juggler-native MCP tool** (toplam 97) — motor ayırımlı kategori dosyaları (`kahin/tools/`):
  - DOM (12): `kahin_mirage_query`, `kahin_mirage_query_all`, `kahin_mirage_click`, `kahin_mirage_type`, `kahin_mirage_get_text`, `kahin_mirage_get_attribute`, `kahin_mirage_set_attribute`, `kahin_mirage_focus`, `kahin_mirage_hover`, `kahin_mirage_get_html`, `kahin_mirage_wait_selector`, `kahin_mirage_get_value`
  - Input (7): mouse_click, mouse_move, mouse_down, mouse_up, key_press, key_text, scroll
  - PageEx (6): reload, go_back, go_forward, stop, frame_tree, page_content
  - Tab/Session (6): tab_new, tab_switch, tab_close, tab_list, tab_bring_front, context_new
  - Network/Console (10): network_requests, get_response_body, intercept_requests, unintercept_requests, network_continue, network_abort, cache_disable, clear_cache, console_log, errors_list
  - Storage (6): cookie_get, cookie_set, cookie_clear, storage_local_get, storage_local_set, storage_session_get
  - Emulation (10): set_user_agent, set_viewport, set_device_scale_factor, set_media, set_touch, set_color_scheme, set_reduced_motion, set_locale, set_timezone, set_geolocation
  - Dialog/Download/Worker/WS (7): dialog_list, dialog_accept, dialog_dismiss, download_list, download_save, worker_list, websocket_list
  - Engine (1): `kahin_engine_health`
- **Phantom liveness sistemi**: `is_alive`/`on_death`, boot'ta `Browser.health` doğrulaması, ölü engine otomatik eviction + state temizliği
- `Mirage.call(method, params, session_id)` API'si — session yönetimi (`create_page/close_page/switch_page/list_pages`)
- `tests/test_phantom.py`, `tests/test_e2e_mirage.py` (gerçek Camoufox e2e: DOM, tab, cookie, dialog, kill, localStorage, emulation)
- pytest-asyncio dev-dep

### Değişen
- Sidecar wire Juggler-native: `{"id","method","params","sessionId?"}` — CDP-şekilli `domain/command` katmanı kaldırıldı
- No-op `Network.enable`/`Console.enable` handler'ları silindi; event adları verbatim forward
- `Network.getResponseBody` → `{base64body, evicted?}`; `Page.dispatchKeyEvent` zorunlu `repeat:bool`
- Screenshot: gerçek viewport ölçümü + `full_page` full-content clip
- Result-less Juggler reply'ları (`{"id":N}`) artık boş `result` olarak kabul ediliyor
- `Page.reload` frameId ile gönderiliyor; frame tree `d.getFrameTree` (gerçek iç içe hiyerarşi)
- oracle.py bootstrap-only — tool tanımları `kahin/tools/` kategori modüllerinde
- Versiyonlar senkronize: package.json / pyproject.toml / `kahin.__version__`

### Düzeltilen
- `@intCast(id)` trap riski (negatif/oversize id → `-32600`)
- `newPage`/`screenshot` hata yutma — artık gerçek hata yanıtı
- devicePixelRatio override: `Page.setViewportSize` yerine `Browser.setDefaultViewport`
- `engine._proc` → `_process` (shadow health pid artık gerçek)
- Sidecar process HUP'ta temiz exit

## [0.1.8] — 2026-08-02

### Değişen
- npm paketi GitLab'a taşındı + GitLab CI publish eklendi (`scripts/npm-publish.sh`, `.gitlab-ci.yml`)
- Versiyon farkı kontrolüyle otomatik yayın

## [0.1.7] — 2026-08-01

### Eklenen
- GitHub auto-publish workflow (`.github/workflows/publish.yml`)
- One-command install: 22 AI CLI istemcisini otomatik tespit + MCP kaydı

## [0.1.0–0.1.6] — 2026-08-01

### Eklenen
- Global npm launcher + auto-setup
- Shadow engine: gerçek Obscura binary (auto-install cascade)
- Mirage engine: gerçek Camoufox via Zig sidecar IPC (Faz 5)
- Zig Juggler harness: pipe transport, Browser/Page/Runtime/Network/Input/Emulation adapters, interception, process manager, perf + crash-recovery
- CDP ansiklopedisi: 56 domain, 667 komut, 237 event, 609 type (Chrome 148)

[0.3.4]: https://gitlab.com/void0x14/kahin-mcp/-/compare/v0.3.3...v0.3.4
[0.3.5]: https://gitlab.com/void0x14/kahin-mcp/-/compare/v0.3.4...v0.3.5
[0.3.3]: https://gitlab.com/void0x14/kahin-mcp/-/compare/v0.3.2...v0.3.3
[0.3.2]: https://gitlab.com/void0x14/kahin-mcp/-/compare/v0.3.1...v0.3.2
[0.2.0]: https://gitlab.com/void0x14/kahin-mcp/-/compare/v0.1.8...v0.2.0
[0.1.8]: https://gitlab.com/void0x14/kahin-mcp/-/compare/v0.1.7...v0.1.8
[0.1.7]: https://gitlab.com/void0x14/kahin-mcp/-/compare/v0.1.6...v0.1.7
[0.3.6]: https://gitlab.com/void0x14/kahin-mcp/-/compare/v0.3.5...v0.3.6
[0.3.7]: https://gitlab.com/void0x14/kahin-mcp/-/compare/v0.3.6...v0.3.7
[0.3.8]: https://gitlab.com/void0x14/kahin-mcp/-/compare/v0.3.7...v0.3.8
[0.3.9]: https://gitlab.com/void0x14/kahin-mcp/-/compare/v0.3.8...v0.3.9
[0.3.10]: https://gitlab.com/void0x14/kahin-mcp/-/compare/v0.3.9...v0.3.10
