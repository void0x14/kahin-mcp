# Tarayıcısız Cloudflare çözümü — `kahin/addons/cf`

Cloudflare'ın interstitial'ı bir **web sayfasıdır**: sayfa `window._cf_chl_opt`
tohumunu içerir, obfuscate edilmiş bir "orchestrate" paketi yükler, paket
kendi sensörlerini toplar ve `/cdn-cgi/challenge-platform/*` adreslerine
POST atar. Sonunda `cf_clearance` çerezi yazılır.

Paket V8 gerektirmez. Gereken şey bir DOM, bir saat ve bir ağdır. Bu dizin
tam olarak onu sağlar — `nox` eklentisinin aynı deseni:

| parça | iş |
|---|---|
| `kahin/addons/cf/shim.js` | challenge sayfasının ihtiyaç duyduğu DOM/window yüzeyi |
| `kahin/addons/cf/cfhost.c` | QuickJS host'u — paketi gömülü motorunda koşturur |
| `kahin/cf_cookie.py` | sürücü: paketin network isteklerini gerçek yanıtlarla karşılar |

## Neden tarayıcı gerekmiyor

Cloudflare paketi bir uygulama değil, bir betik. Betiğin ihtiyaç duyduğu yüzey
sabit ve dar:

* `document.readyState` yaşam döngüsü (`loading` → `interactive` →
  `active` → `complete`) — paket **ne zaman başlayacağına** bununla karar verir,
* `window` / `self` / `top` / `parent` **aynı nesne** olmalı (kimlik
  karşılaştırması yapılır),
* `document.location` ile `window.location` de aynı nesne,
* `navigator.cookieEnabled`, `BigInt`, `crypto.getRandomValues`,
  `performance.getEntries`, `PerformanceObserver`,
  `ReadableStream.prototype.pipeTo`, `URL.createObjectURL` — hepsi
  "tarayıcı destekliyor mu" kontrolü,
* `onload` / `onreadystatechange` gibi özellik-handler'ları da gerçek
  dinleyicidir,
* `document.getElementById` / `querySelector` gerçekten çalışır,
* `script.src` özelliği atandığında paket indirilip **global kapsamda**
  çalıştırılır.

Bu yüzeyin büyük kısmı `shim.js`de var. Sayfanın kendi HTML'i de önce
DOM'a çevrilir — paket kendi işaretlemesini okuyor (`.main-content`,
`.main-wrapper`).

## Akış

```
1. sürücü challenge sayfasını indirir (curl_cffi, chrome131 profili)
2. sayfadaki `window._cf_chl_opt` bootstrap'ı çıkarılır
3. cfhost: bootstrap + sayfa HTML'i shim'e yüklenir
4. paket `/cdn-cgi/.../orchestrate/chl_page/v1` isteğini yapar
5. sürücü isteği gerçekten yapar, yanıtı host'a geri verir
6. paket `challenges.cloudflare.com/turnstile/.../api.js` ister, aynı yol
7. widget kendi iframe'ini kurar (canvas/worker gerektiren katman)
```

7. adımda Cloudflare'ın Turnstile widget'ı devreye girer; widget gerçek bir
tarayıcı istiyorsa motor yolu `cf_clearance` üretemez. Bu durumda
`kahin_cf_clear` **tarayıcı yoluna** düşer (kahin's headless Camoufox).

## Ölçülen değerler

| Ölçüm | Değer |
|---|---|
| Paket yükleme (challenge + orchestrate + turnstile api.js) | ~370 ms |
| Motor yolunun toplam süresi (başarısız) | ~0.4 s |
| Host binary boyutu | ~1.4 MB |
| Peak RSS | ~12 MB |

## Dürüst sınır

Motor yolu **başarıyı kanıtlamaz**: `cf_clearance` çerezi çıkmazsa
`cleared: false` döner ve sebebi raporlanır. Başarı ölçütü sayfanın kendisidir
— çerez yeniden yüklendiğinde interstitial gelmemelidir
(`docs/cf-hata-mezarligi.md` M11).

## Kurulum

```
python -m kahin.cf_cookie --build      # host binary'sini derler
python -m kahin.cf_cookie --url https://pastebin.com/login
```

Kurulum sırasında `bin/setup.mjs` QuickJS'i indirip host'u derler. Derleme
araçları yoksa atlanır ve yalnızca tarayıcı yolu çalışır. `KAHIN_SKIP_CF=1`
ile açıkça kapatılır.

## Backshot entegrasyonu

Motor `cf_clearance`'ı `$KAHIN_HOME/cf/<host>.cookie` dosyasına yazar:

```
{
  "host": "pastebin.com",
  "name": "cf_clearance",
  "value": "...",
  "minted_at": 1793769067.0,
  "user_agent": "...",
  "verified": true,
  "source": "browser"
}
```

Scraper dosyayı okur, `cf_clearance`'ı `Cookie` başlığına koyar ve
`pastebin.com`'a gider.
