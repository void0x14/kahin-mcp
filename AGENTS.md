# Kahin — CDP MCP Server Kullanım Kılavuzu

Bu MCP server, Chrome DevTools Protocol (CDP) bilgisi, doğrulaması ve browser kontrolü sağlar.
56 domain, 667 komut, 237 event, 609 type — Chrome 148.

## Tool İsimlendirme

OpenCode'da tool isimleri `kahin_` prefix'i ile başlar. AGENTS.md'deki isimler aynen kullanılır:

```
kahin_list_domains       → OpenCode: kahin_list_domains
kahin_get_command        → OpenCode: kahin_get_command
kahin_browser_start      → OpenCode: kahin_browser_start
```

## Guardian Sistemi — Otomatik Hata Yakalama ve Eğitim

Kahin MCP, AI modelin CDP hatalarını **otomatik yakalar** ve **doğrusunu öğretir**.

### Nasıl Çalışır?

```
AI: kahin_execute_cdp(domain="Page", command="navigat", parameters={"url": "..."})
    ↓ (otomatik validation)
MCP: {
  "error": "Command validation failed",
  "validation_errors": [{"param": "navigat", "message": "Unknown command 'Page.navigat'. Did you mean 'navigate'?"}],
  "correction": ["Typo: Page.navigat → Page.navigate"]
}
    ↓ (AI hatasını anlar, düzeltir)
AI: kahin_execute_cdp(domain="Page", command="navigate", parameters={"url": "..."})
    → {"frameId": "...", "loaderId": "..."}
```

### AI Model İçin Kural
Sakın tahmin etme. CDP komutunda en ufak şüphen varsa:
1. `kahin_validate_command` ile kontrol et
2. Hata alırsan `kahin_error_decode` ile çözümle
3. Pattern varsa `kahin_pattern_query` ile öğren

## ZORUNLU Kullanım Kuralları

### Kural 1: CDP komutu göndermeden ÖNCE doğrula
```
YANLIŞ: Page.navigate(url="...")                    # typo riski
DOĞRU:  kahin_validate_command(domain="Page", command="navigate", parameters={"url": "..."})
        → {"valid": true, ...}
        → Page.navigate(url="...")
```

### Kural 2: Hata alınca çözümle
```
kahin_error_decode(error_code=-32601, error_message="Method not found: Page.navigat")
→ "Typo in method name: Page.navigat should be Page.navigate"
```

### Kural 3: Bilmediğin CDP'yi sorgula
```
kahin_get_command(domain="Page", command="navigate")
→ {"parameters": [{"name": "url", "type": "string", ...}], ...}
```

### Kural 4: Pattern'ları sorgula ve öğren
```
kahin_pattern_query(context="doggystyle")
kahin_pattern_suggest(partial="navig")
```

## Tool Listesi (155 adet)

Juggler/Mirage yüzeyinin ajana dönük, uçtan uca sözleşmesi:
[docs/juggler-ai-native.md](docs/juggler-ai-native.md)

### GRIMOIRE — CDP Bilgi (7)
| Tool | Ne işe yarar? |
|------|---------------|
| `kahin_list_domains` | Tüm domainleri listele (56 adet) |
| `kahin_get_domain` | Domain içindeki komut/event/type'ları göster |
| `kahin_get_command` | Komut parametrelerini ve dönüşlerini göster |
| `kahin_get_event` | Event parametrelerini göster |
| `kahin_find_concept` | Doğal dille CDP konsepti ara (ör: "take screenshot") |
| `kahin_list_types` | Domain'deki type'ları listele |
| `kahin_get_type` | Type property'lerini ve enum değerlerini göster |

### SERAPH — Doğrulama (3)
| Tool | Ne işe yarar? |
|------|---------------|
| `kahin_validate_command` | CDP komutunu göndermeden ÖNCE doğrula (typo tespit eder!) |
| `kahin_error_decode` | CDP hata kodunu çözümle, alternatif öner |
| `kahin_get_dependencies` | Komutun ön koşullarını göster |

### PILOT — Browser Kontrol (9)
| Tool | Ne işe yarar? |
|------|---------------|
| `kahin_browser_start` | Varsayılan Camoufox/Mirage browser motorunu başlat (Shadow açıkça seçilebilir) |
| `kahin_browser_stop` | Browser'ı durdur, state temizle |
| `kahin_navigate` | URL'e git |
| `kahin_click` | CSS selector ile element tıkla |
| `kahin_extract` | Sayfadan metin/attribute çek |
| `kahin_screenshot` | Ekran görüntüsü al (base64 PNG) |
| `kahin_evaluate` | JavaScript çalıştır |
| `kahin_execute_cdp` | Ham CDP komutu gönder (ileri seviye) |
| `kahin_ocr` | Görüntüyü Google Vision TEXT_DETECTION ile oku |

### TRAINMAN — Session (4)
| Tool | Ne işe yarar? |
|------|---------------|
| `kahin_list_sessions` | Açık target'ları listele |
| `kahin_get_session` | Session detayını göster |
| `kahin_create_session` | Yeni sayfa/target oluştur |
| `kahin_kill_session` | Target'ı kapat |

### DEJA_VU — Debug (4)
| Tool | Ne işe yarar? |
|------|---------------|
| `kahin_event_history` | CDP event geçmişini göster (filtreleme destekler) |
| `kahin_list_network_requests` | Network isteklerini listele |
| `kahin_get_console` | Console mesajlarını göster |
| `kahin_iframe_tree` | Frame/iframe hiyerarşisini göster |

### PROPHECY — Pattern DB (5)
| Tool | Ne işe yarar? |
|------|---------------|
| `kahin_pattern_learn` | Yeni pattern öğret |
| `kahin_pattern_query` | Pattern ara (domain/context filtresi) |
| `kahin_pattern_suggest` | Kısmi isimle autocomplete |
| `kahin_pattern_forget` | Pattern sil |
| `kahin_pattern_stats` | Pattern istatistikleri |

### MIRAGE — Juggler Native (106) — Camoufox varsayılandır; Shadow'dan gerektiğinde otomatik yükseltilir

#### DOM Stream (5) — gerçek MutationObserver + Juggler binding
| Tool | Ne işe yarar? |
|------|---------------|
| `kahin_mirage_dom_start` | Gerçek-zamanlı DOM observer'ı mevcut ve sonraki document'lara kurar |
| `kahin_mirage_dom_snapshot` | Bounded, anlamsal, action ipuçlu canlı DOM ağacı döndürür |
| `kahin_mirage_dom_events` | Cursor/streamId ile mutation ve input/focus/click event delta'larını okur; long-poll destekler |
| `kahin_mirage_dom_action` | Snapshot'tan alınan canlı nodeId üzerinde click/hover/focus/type/scroll/select yapar |
| `kahin_mirage_dom_stop` | Mevcut frame observer'ını durdurur; sonraki DOM çağrısı yeni streamId ile yeniden kurar, eski nodeId/cursor geçersiz olur |

#### DOM (13) — hepsinde opsiyonel `frame_id` parametresi (iframe içi erişim; listeleme: `kahin_mirage_frame_tree`)
| Tool | Ne işe yarar? |
|------|---------------|
| `kahin_mirage_query` | CSS selector ile ilk elementi bul |
| `kahin_mirage_query_all` | CSS selector ile tüm elementleri bul |
| `kahin_mirage_click` | Elemente tıkla (gerçek fare olayı) |
| `kahin_mirage_type` | Elemente metin yaz (Page.insertText) |
| `kahin_mirage_get_text` | Elementin görünen metnini al |
| `kahin_mirage_get_attribute` | Element attribute'ünü al |
| `kahin_mirage_set_attribute` | Element attribute'ü set et |
| `kahin_mirage_focus` | Elemente odaklan |
| `kahin_mirage_hover` | Elementin üzerine gel |
| `kahin_mirage_get_html` | Elementin outerHTML'ini al |
| `kahin_mirage_wait_selector` | Seçici görünene kadar bekle (timeout) |
| `kahin_mirage_get_value` | Input elementinin değerini al |
| `kahin_mirage_eval` | Serbest JS ifadesi çalıştır (opsiyonel frame_id; ana frame master world'de shadowRootUnl + cross-origin contentDocument okunur) |

#### Reliability (9) — locator, assertion, verified input ve route araçları
| Tool | Ne işe yarar? |
|------|---------------|
| `kahin_mirage_expect` | Web-first assertion'ı retry ile doğrular |
| `kahin_mirage_check` / `kahin_mirage_uncheck` | Checkbox/radio durumunu değiştirir ve doğrular |
| `kahin_mirage_select_option` | Gerçek `<select>` option'ını seçer ve doğrular |
| `kahin_mirage_dblclick` | Gerçek çift tıklama gönderir |
| `kahin_mirage_drag` | Bounded mouse adımlarıyla drag-and-drop yapar |
| `kahin_mirage_wait_for_text` | Sayfa metni görünene kadar bekler |
| `kahin_mirage_wait_for_timeout` | Bounded millisecond bekleme yapar |
| `kahin_mirage_route` | Eşzamanlı request-producing çağrıyla bekleyen tek sonraki isteği abort/continue/fulfill eder |

#### Input (7)
| Tool | Ne işe yarar? |
|------|---------------|
| `kahin_mirage_mouse_click` | Koordinatta fare tıklaması |
| `kahin_mirage_mouse_move` | Fareyi koordinata taşı |
| `kahin_mirage_mouse_down` / `kahin_mirage_mouse_up` | Fare butonu bas/bırak |
| `kahin_mirage_key_press` | Tuş kombinasyonu gönder (keyDown+keyUp) |
| `kahin_mirage_key_text` | Metni tuşlarla yaz |
| `kahin_mirage_scroll` | Sayfayı kaydır (wheel) |

#### PageEx (6)
| Tool | Ne işe yarar? |
|------|---------------|
| `kahin_mirage_reload` | Sayfayı yenile |
| `kahin_mirage_go_back` / `kahin_mirage_go_forward` | Geçmişte geri/ileri git |
| `kahin_mirage_stop` | Sayfa yüklemeyi durdur (window.stop) |
| `kahin_mirage_frame_tree` | Frame hiyerarşisini göster |
| `kahin_mirage_page_content` | Tüm sayfa HTML'ini al |

#### Tab/Session (7)
| Tool | Ne işe yarar? |
|------|---------------|
| `kahin_mirage_tab_new` | Yeni sayfa/sekme aç |
| `kahin_mirage_tab_switch` | Hedef sekmeye geç |
| `kahin_mirage_tab_close` | Sekmeyi kapat |
| `kahin_mirage_tab_list` | Açık sekmeleri listele |
| `kahin_mirage_tab_bring_front` | Sekmeyi öne getir |
| `kahin_mirage_context_new` | Yeni browser context oluştur |
| `kahin_mirage_context_close` | İzole browser context'i ve içindeki sekmeleri kapat |

#### Network/Console (10)
| Tool | Ne işe yarar? |
|------|---------------|
| `kahin_mirage_network_requests` | Network isteklerini listele (event buffer) |
| `kahin_mirage_get_response_body` | Yanıt gövdesini al (base64 decode) |
| `kahin_mirage_intercept_requests` / `kahin_mirage_unintercept_requests` | İstek yakalamayı aç/kapat |
| `kahin_mirage_network_continue` | Yakalanan isteği sürdür (override) |
| `kahin_mirage_network_abort` | Yakalanan isteği iptal et |
| `kahin_mirage_cache_disable` | HTTP cache'i kapat/aç |
| `kahin_mirage_clear_cache` | Tarayıcı cache'ini temizle |
| `kahin_mirage_console_log` | Console mesajlarını göster (Runtime.console) |
| `kahin_mirage_errors_list` | Yakalanmamış sayfa hatalarını göster |

#### Storage (6)
| Tool | Ne işe yarar? |
|------|---------------|
| `kahin_mirage_cookie_get` / `kahin_mirage_cookie_set` / `kahin_mirage_cookie_clear` | Çerez oku/yaz/temizle (Browser.*) |
| `kahin_mirage_storage_local_get` / `kahin_mirage_storage_local_set` | localStorage oku/yaz |
| `kahin_mirage_storage_session_get` | sessionStorage oku |

#### Emulation (10)
| Tool | Ne işe yarar? |
|------|---------------|
| `kahin_mirage_set_user_agent` | User-Agent override (Browser.setUserAgentOverride) |
| `kahin_mirage_set_viewport` | Viewport boyutu + opsiyonel devicePixelRatio (Browser.setDefaultViewport; isMobile Juggler'da yok, kaldırıldı) |
| `kahin_mirage_set_device_scale_factor` | devicePixelRatio override (Browser.setDefaultViewport, mevcut viewport korunur) |
| `kahin_mirage_set_media` | Medya tipi emule (screen/print) |
| `kahin_mirage_set_touch` | Touch desteği emule |
| `kahin_mirage_set_color_scheme` | prefers-color-scheme emule |
| `kahin_mirage_set_reduced_motion` | prefers-reduced-motion emule |
| `kahin_mirage_set_locale` / `kahin_mirage_set_timezone` | Locale/timezone override |
| `kahin_mirage_set_geolocation` | Konum emule |

#### Dialog/Download/Worker/WS (7)
| Tool | Ne işe yarar? |
|------|---------------|
| `kahin_mirage_dialog_list` | Açık dialog'ları listele (event buffer) |
| `kahin_mirage_dialog_accept` / `kahin_mirage_dialog_dismiss` | Dialog kabul et/reddet (Page.handleDialog) |
| `kahin_mirage_download_list` / `kahin_mirage_download_save` | İndirmeleri listele / diske kaydet |
| `kahin_mirage_worker_list` | Web worker'ları listele |
| `kahin_mirage_websocket_list` | WebSocket'leri listele |

#### Engine (2)
| Tool | Ne işe yarar? |
|------|---------------|
| `kahin_engine_health` | Çalışan motor sağlığı (Mirage: Browser.health) |
| `kahin_engine_stats` | Monotonic uptime + bounded per-tool performans rollup'ı (`tool_calls`/`tool_errors`, `top_slow` ≤ 10, `last_error`); engine yoksa yapılandırılmış `engine_unavailable` |

#### Upload (2)
| Tool | Ne işe yarar? |
|------|---------------|
| `kahin_mirage_set_file_chooser_intercept` | File chooser interception'ı aç/kapat (Page.setInterceptFileChooserDialog) |
| `kahin_mirage_upload_files` | Page.fileChooserOpened bekle + Page.setFileInputFiles; input önceden/eşzamanlı tıklanmalı, absolute path zorunlu |

#### Screencast (4) + Watch (2)
| Tool | Ne işe yarar? |
|------|---------------|
| `kahin_mirage_screencast_start` | Canlı ekran kaydı başlat (Page.startScreencast → screencastId) |
| `kahin_mirage_screencast_frame` | Bir sonraki frame'i al (base64 JPEG) + otomatik ack; sayfa değişiminden sonra `fresh=true` ile kuyruk temizle |
| `kahin_mirage_screencast_stop` | Kaydı durdur, kalan frame'leri temizle |
| `kahin_mirage_screencast_pending` | Bekleyen (ack'siz) frame sayısı + stream sağlığı |
| `kahin_mirage_watch_start` | Aktif screencast için localhost-only MJPEG watch başlat |
| `kahin_mirage_watch_stop` | MJPEG server ve pump'u durdur (screencast çalışmaya devam eder) |

#### Accessibility (1)
| Tool | Ne işe yarar? |
|------|---------------|
| `kahin_mirage_accessibility_tree` | Erişilebilirlik ağacı (Accessibility.getFullAXTree — Camoufox-only, CDP'de yok) |

#### Agent (11) — ref'li snapshot, form doldurma, oturum/kimlik kalıcılığı, durum ve crawl challenge özeti
| Tool | Ne işe yarar? |
|------|---------------|
| `kahin_mirage_snapshot` | Canlı DOM ağacını token bütçeli, ref'li satırlara çevirir (`[ref=n9]` ile action ipuçlu) |
| `kahin_mirage_fill_form` | Ref'lerle birden fazla alanı tek çağrıda doldurur; stale ref `requiresSnapshot` döner |
| `kahin_mirage_state_save` / `kahin_mirage_state_load` | Oturumu (url + cookie + local/sessionStorage) mutlak yola kaydeder/geri yükler |
| `kahin_identity_new` / `kahin_identity_save` / `kahin_identity_list` / `kahin_identity_delete` | Camoufox fingerprint kimliklerini oluştur/kaydet/listele/sil |
| `kahin_identity_report` | Aktif engine'in kimlik özetini ve sayfa-içi canlı `navigator.userAgent`'ı raporlar |
| `kahin_agent_status` | Agent döngüsü özeti: engine liveness, sayfa durumu, sekme sayısı, refsLive/domCursor, dialog/network/console sayaçları |
| `kahin_challenge_status` | CAPTCHA/access-denied/rate-limit algılar; `retryAfterSeconds` ve güvenli pause/backoff kararını döndürür; bypass etmez |

#### Stealth (9) — anti-detect denetimi, insansı girdi, kimlik rotasyonu ve proxy/geo
| Tool | Ne işe yarar? |
|------|---------------|
| `kahin_stealth_audit` | Salt-okunur leak probe paketi (14 check); skor `{passed, total, ratio}` ile döner, hiçbir check atlanmaz |
| `kahin_mirage_mouse_trajectory` | Jitter'lı Bézier fare yörüngesi (steps ≤ 200, jitter ≤ 20px, seed'li deterministik) |
| `kahin_mirage_click_humanized` | Yörüngeli, insansı gecikmeli gerçek DOM tıklaması |
| `kahin_mirage_key_text` (çapraz liste) | Jitter'lı tuş cadence'i ile metin yazma (delay_ms=0 → hızlı yol); Input (7) altında sayılır — tek kayıt, Stealth sayımına dahil değil |
| `kahin_identity_pin` / `kahin_identity_unpin` / `kahin_identity_pins` / `kahin_identity_for_domain` | Domain başına identity rotasyon politikası (bounded, doğrulanmış `~/.config/kahin/pins.json`) |
| `kahin_fingerprint_report` | Canlı sayfa evaluate'sinden sitenin göreceği fingerprint (UA/platform/screen/WebGL; emülasyon onayından uydurulmaz) |
| `kahin_proxy_resolve` | Proxy exit-IP geo + timezone/locale/geolocation önerisi; URL'deki kimlik bilgileri asla yankılanmaz |

Stealth (9) sayımı yalnızca Stealth-native araçları içerir; `kahin_mirage_key_text`
çapraz listelenmiştir ve sayıma Input (7) altında girer (MCP yüzeyinde tek kayıt).

Stealth CI kapısı: `KAHIN_REQUIRE_STEALTH=1` altında audit ratio ≥ 0.8 ve
identity rotasyonu tam fingerprint özetini değiştirmek zorundadır
(`scripts/stealth-regression.py` drift-watcher + `camoufox-harness/tests/perf/stealth-baseline.json`).

### ORBIT — Long Crawler (7)

| Tool | Ne işe yarar? |
|------|---------------|
| `kahin_crawl_start` | Tek Mirage browser/tab üzerinde bounded background crawler job başlatır; varsayılan rotation 20 sayfa veya 900 saniyedir |
| `kahin_crawl_status` | Job state, queue, sonuç, challenge, rotation, recovery ve engine health özetini döndürür |
| `kahin_crawl_results` | Opaque cursor ile en fazla 100 bounded crawl sonucunu döndürür |
| `kahin_crawl_events` | Bounded monotonic cursor ve opsiyonel long-poll ile canlı crawl ilerleme delta'larını okur |
| `kahin_crawl_pause` / `kahin_crawl_resume` | Challenge sonrası açık insan/provider kararıyla job'ı durdurur/devam ettirir; bypass yapmaz |
| `kahin_crawl_stop` | Background job'ı durdurur; browser'ı otomatik kapatmaz |

Crawler tek engine slotunu korur; ikinci browser veya gizli crawler tabı açmaz.
429/503 için Retry-After ve bounded backoff uygular. CAPTCHA/access-denied
durumunda `paused` kalır; resume challenge çözmez ve identity rotation'ı
rate-limit kaçış yolu olarak kullanmaz.
### VISUALIZATION (1)

| Tool | Ne işe yarar? |
|------|---------------|
| `kahin_visualize_data` | Bounded satırlardan deterministik line/bar/scatter/pie SVG grafiği ve özet üretir |

### EXTENSIONS (1)

| Tool | Ne işe yarar? |
|------|---------------|
| `kahin_extension_prepare` | Gerçek WebExtension'ı güvenle stage eder ve Camoufox yerel uyumluluk raporunu döner |

### CF-CLEAR — Cloudflare (2)

| Tool | Ne işe yarar? |
|------|---------------|
| `kahin_cf_clear` | Mevcut oturumda Cloudflare challenge'ı temizle (embedded solver; interstitial title gate + host-scoped `cf_clearance` doğrular, tıklayamıyorsa `pause_for_human` döner) |
| `kahin_cf_status` | Mevcut sayfanın Cloudflare clearance durumunu raporla (salt-okunur; gezinmez, tıklamaz) |

### Faz 4 — Performans (Zig sidecar + metrik yüzeyi)

- **Non-blocking sidecar**: Zig sidecar artık tek-in-flight değil; stdin
  işleme browser yanıtını asla bloklamaz, birden fazla istek aynı anda
  in-flight olabilir (request state machine, poll güdümlü loop, `max_inflight`
  cap; IPC sözleşmesi/event forwarding/id-matching değişmedi). N=20
  `Runtime.evaluate` probe'u: seri 74.78 → 28.56 ms, paralel duvar süresi
  56.89 → 8.71 ms (6.5× düşüş), ratio 1.314 → 3.281 (plan gate'i: ratio >
  1.5) — `camoufox-harness/tests/perf/concurrency.md`.
- **`kahin_engine_stats`**: monotonic clock ile ölçülen uptime + healer
  tracker'dan per-tool agregatlar (`{calls, errors, total_ms, max_ms}`, en
  fazla 256 tool, `top_slow` en yavaş 10'u); `safe()` ölçümleri
  `time.monotonic()`'a taşındı; engine yoksa structured `engine_unavailable`
  yanıtı, asla hata fırlatmaz.
- **Identity profile prewarm (dürüst sınır)**: per-identity başına **bounded
  metadata/reuse kaydı** — stabil sha256 identity hash (16 hex), in-process
  cache (max 8, FIFO eviction) + `~/.cache/kahin/profiles/<hash>.json`
  (≤ 4 KiB yazma, 8 KiB üstü okuma reddi, tmp+rename, bozuk dosya → miss);
  `engine_stats.prewarm` `{options_ms, profile_ms, hits, starts, cache}`
  döner. **Gerçek launch işi asla atlanmaz**: `launch_options` her start'ta
  kasıtlı per-launch rastgelelik ürettiği için yeniden çağrılır ve runtime
  profil dizini yeniden kullanılmaz (önceki oturum cookie/session verisini
  geri yüklerdi). Metadata kimlik payload'ı içermez.

## Örnek İş Akışları

### 1. CDP Komutu Araştırma + Doğrulama + Gönderme
```
→ kahin_find_concept(query="navigate to url", max_results=3)
→ kahin_get_command(domain="Page", command="navigate")
→ kahin_validate_command(domain="Page", command="navigate", parameters={"url": "..."})
→ (komut güvenle gönderilir)
```

### 2. Browser Aç + Sayfaya Git + İçerik Çek
```
→ kahin_browser_start(headless=true)
→ kahin_navigate(url="https://github.com/void0x14/doggystyle")
→ kahin_extract()                              -> tüm sayfa metni
→ kahin_screenshot()                           -> ekran görüntüsü
→ kahin_browser_stop()
```

### 3. Hata Ayıklama
```
→ kahin_error_decode(error_code=-32601, error_message="Method not found: Page.navigat")
  -> "Typo: Page.navigat → Page.navigate"
→ kahin_get_dependencies(domain="Fetch", command="enable")
  -> "Must call Fetch.enable to activate the domain"
```

### 4. Pattern Kullanımı
```
→ kahin_pattern_stats()                          -> mevcut pattern'lar
→ kahin_pattern_learn(domain="Page", command="navigate", context="doggystyle")
→ kahin_pattern_query(context="doggystyle")      -> öğrenilenler
```

## Tool Listesi (Inspector Dogrulamali)

32 tool MCP Inspector ile dogrulanmistir:
```
kahin_list_domains          kahin_get_domain            kahin_get_command
kahin_get_event             kahin_find_concept          kahin_list_types
kahin_get_type              kahin_validate_command      kahin_error_decode
kahin_get_dependencies      kahin_browser_start         kahin_browser_stop
kahin_navigate              kahin_click                 kahin_extract
kahin_screenshot            kahin_evaluate              kahin_execute_cdp
kahin_list_sessions         kahin_get_session           kahin_create_session
kahin_kill_session          kahin_event_history         kahin_list_network_requests
kahin_get_console           kahin_iframe_tree           kahin_pattern_learn
kahin_pattern_query         kahin_pattern_suggest       kahin_pattern_forget
kahin_pattern_stats         kahin_healer_stats
```

65 MIRAGE (Juggler native) tool Faz 9 Task 3'te eklendi (test_phantom +
test_mirage_ipc ile doğrulandı):
```
kahin_mirage_query           kahin_mirage_query_all      kahin_mirage_click
kahin_mirage_type            kahin_mirage_get_text       kahin_mirage_get_attribute
kahin_mirage_set_attribute   kahin_mirage_focus          kahin_mirage_hover
kahin_mirage_get_html        kahin_mirage_wait_selector  kahin_mirage_get_value
kahin_mirage_mouse_click     kahin_mirage_mouse_move     kahin_mirage_mouse_down
kahin_mirage_mouse_up        kahin_mirage_key_press      kahin_mirage_key_text
kahin_mirage_scroll          kahin_mirage_reload         kahin_mirage_go_back
kahin_mirage_go_forward      kahin_mirage_stop           kahin_mirage_frame_tree
kahin_mirage_page_content    kahin_mirage_tab_new        kahin_mirage_tab_switch
kahin_mirage_tab_close       kahin_mirage_tab_list       kahin_mirage_tab_bring_front
kahin_mirage_context_new     kahin_mirage_network_requests kahin_mirage_get_response_body
kahin_mirage_intercept_requests kahin_mirage_unintercept_requests kahin_mirage_network_continue
kahin_mirage_network_abort   kahin_mirage_cache_disable  kahin_mirage_clear_cache
kahin_mirage_console_log     kahin_mirage_errors_list    kahin_mirage_cookie_get
kahin_mirage_cookie_set      kahin_mirage_cookie_clear   kahin_mirage_storage_local_get
kahin_mirage_storage_local_set kahin_mirage_storage_session_get kahin_mirage_set_user_agent
kahin_mirage_set_viewport    kahin_mirage_set_device_scale_factor kahin_mirage_set_media
kahin_mirage_set_touch       kahin_mirage_set_color_scheme kahin_mirage_set_reduced_motion
kahin_mirage_set_locale      kahin_mirage_set_timezone   kahin_mirage_set_geolocation
kahin_mirage_dialog_list     kahin_mirage_dialog_accept  kahin_mirage_dialog_dismiss
kahin_mirage_download_list   kahin_mirage_download_save  kahin_mirage_worker_list
kahin_mirage_websocket_list  kahin_engine_health
```

Faz 10 gap kapatma (gerçek HTTP e2e + iframe + upload + screencast + a11y) sonrası eklendi:
```
kahin_mirage_set_file_chooser_intercept kahin_mirage_upload_files
kahin_mirage_screencast_start   kahin_mirage_screencast_frame
kahin_mirage_screencast_stop    kahin_mirage_screencast_pending
kahin_mirage_accessibility_tree
```

Gerçek-zamanlı DOM stream Faz 11'de eklendi. `dom_start`'ten alınan `streamId`
ve `dom_snapshot` cursor'u ile `dom_events` çağrısı yapılır; `streamId`
verilmezse navigation algılanamaz. `reset`/`dropped`/`requiresSnapshot`
durumlarında ajan yeni snapshot almak zorundadır. NodeId document/frame
ömrüyle sınırlıdır; CSS selector yerine canlı nodeId ile action yapılır.

## Önemli Notlar
1. CDP **case-sensitive**: `Page.navigate` ✓, `page.navigate` ✗
2. Port 9222 (Chrome DevTools) ve 9240 REZERVE — kullanma
3. Hatalar `logs/kahin.log` dosyasına JSON formatında kaydedilir
4. `kahin_healer_stats` ile hata istatistiklerini sorgula
5. Pattern'lar otomatik öğrenilir (navigate/click/evaluate sonrası)
