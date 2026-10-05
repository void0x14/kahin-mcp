# WAF çerez üretimi — `nox_jst_v1` (Baidu ADAS)

Bazı siteler (gitee.com ve ADAS arkasındaki diğerleri) istenen sayfayı vermek
yerine bir WAF challenge sayfası döndürür. İstek `nox_jst_v1` çerezini taşımadığı
sürece cevap `405 Not Allowed` olur:

```
HTTP/1.1 405 Not Allowed
Server: ADAS/1.0.214
BDWAF-Request-ID: ...

<script src="/sd5prgymvjlf4cklsqkz91do2mhorb/static/wb/2.1/nox_20260413.js"></script>
<script src="/sd5prgymvjlf4cklsqkz91do2mhorb/static/wb/2.0/gangplank_20251103.js"></script>
```

Bu çerez sitenin kendi `nox_*.js` paketinin imzasıdır. Kahin bu paketi gerçek bir
tarayıcı olmadan çalıştırıp çerezi ~9 ms'de üretir ve bir dosyaya yazar. Böylece
tarayan taraf (ör. backshoot) dosyayı okur, tarama sırasında hiçbir gecikme yaşamaz.

## Kullanım

```
kahin_waf_cookie_mint(origin="https://gitee.com", probe_path="/explore")
kahin_waf_cookie_status()
kahin_waf_cookie_header()
```

Üretilen çerez iki yerde okunur:

```
$ KAHIN_HOME/nox/gitee.com.cookie        # JSON: name, value, minted_at, declared_ttl
kahin_waf_cookie_header()                # "nox_jst_v1=2.0_…" (scraper'a verilecek hazır değer)
```

Komut satırı:

```
python -m kahin.waf_cookie --origin https://gitee.com --probe-path /explore
python -m kahin.waf_cookie --build        # nox host binary'sini derler
```

## Neden tarayıcı gerekmiyor

nox paketi bir JSVMP bytecode motorudur. Gerçek bir tarayıcıdan istediği tek şey
`window.resetNoxJstV1()` fonksiyonu ve yazılabilir bir `document.cookie`. JS kendini
kurar; çerezi otomatik yazmaz, explicit çağrı gerekir.

`kahin/addons/nox/shim.js` bu minimum yüzeyi kurar (yaklaşık 6 KB). Motor,
QuickJS üzerinde çalışır — V8 değil, gömülü bir yorumlayıcı.

## Ölçülen değerler

| Ölçüm | Değer |
|---|---|
| Peak RSS | 10.3-11.4 MB (tek hane) |
| Soğuk başlangıç | 88-135 ms |
| Çerez basma (sıcak) | ~9 ms |
| Tek süreçte üretim | 10/10 benzersiz, spawn yok |
| TTL (canlı ölçüm) | 27-30 dakika |

Süreç yeniden başlatmaya gerekmiyor: `resetNoxJstV1()` tek süreçte 5-15 ms'de yeni
çerez basıyor. 30 dakikada bir process spawn etmenin anlamı kalmıyor.

## Çerez ömrü — neden 20 dakika

Çerez ömrü WAF'ın kendi bildirdiği `window.__noxExpire` değeri (ölçümde 30). Canlı
ölçüm, gerçek sınırı 27-30 dakika olarak gösteriyor:

```
t=1500s status=200
t=1653s status=200
t=1806s status=405
```

Bu yüzden yenileme aralığı TTL'in kendisi değil, `__noxExpire × 0.66` (30 → 1188 s
= 19.8 dakika). 25 dakikalık bir periyot seçilseydi yalnızca 2-5 dakikalık pay
kalırdı; ağ gecikmesi veya WAF kesintisi bu payı yiyebilirdi.

Aralık sabit yazılmaz: `__noxExpire` okunur, WAF TTL'i değiştirirse addon otomatik
uyum sağlar. Beyan yoksa daha uzun, temkinli bir varsayılan kullanılır.

## Test edilen davranışlar

- **UA bağımsız.** Chrome, Firefox, `curl/8.5.0` ve macOS Safari UA'larıyla üretilen
  çerez 200 verdi. Çerez UA'ya bağlı değil.
- **Sadece nox yeterli.** Aynı WAF'ın `tox_token` katmanı bu rotalarda istenmiyor;
  yalnız `nox_jst_v1` ile 200 geliyor.
- **IP bağımlılığı ölçülmedi.** Tek public IP ile çalışıldı. Üretim aynı makinede
  yapıldığı için hedefle uyumlu; çapraz IP senaryosu test edilmedi.
- **Eşzamanlılık.** 8 paralel üretim → 8 benzersiz çerez, hepsi geçerli.
- **Hata ayrımı.** Script sürümü değişirse `nox_api_missing`, bozuk script
  `script_threw`; ikisi de sahte çerez üretmiyor.

## Kırılgan noktalar

**Script URL'leri hardcode edilemez.** Yol öneki dağıtıma göre üretiliyor
(`/sd5prgymvjlf4cklsqkz91do2mhorb/`). Addon challenge HTML'ini okuyup `src`
değerlerini parse ediyor; `__noxExpire`'ı aynı yerden alıyor.

**Challenge rotaya bağlı.**

```
https://gitee.com/           → 200   (koruma yok)
https://gitee.com/explore    → 405
```

Addon `probe_path`'e gitmeli. Kökten script arayamaz.

**Script boyutu challenge sınırına takılır.** nox paketi ~316 KB, challenge sayfası
~400 bayt. İkisi ayrı okuma sınırına bağlıdır.

**Üçüncü taraf JS çalıştırılır.** Bu süreç WAF'ın kendi JavaScript'ini çalıştırır.
Script yalnızca origin'in altından geliyorsa yüklenir; başka bir adrese işaret eden
challenge reddedilir.

## Stealth

Şu an gerekmiyor: çerez UA'dan ve fingerprint'ten bağımsız. Geleceğe dönük bağlama
hazır — host, çerezi `navigator.userAgent` değerinden basıyor ve bu değer `NOX_USER_AGENT`
ile veriliyor. Motor bir kimlik değiştirdiğinde (`kahin_identity_*`) çerez de onunla
tutarlı kalır; shim'de tek satır.

## Kurulum

nox host binary'si platforma özgü derlendiği için wheel içinde taşınamaz. `bin/setup.mjs`
kurulumda QuickJS'i indirip `kahin/addons/nox/bin/noxhost` üretir:

```
git clone --depth 1 https://github.com/quickjs-ng/quickjs.git kahin/addons/nox/_build/quickjs
cmake -B kahin/addons/nox/_build/quickjs/build -DCMAKE_BUILD_TYPE=Release kahin/addons/nox/_build/quickjs
cmake --build kahin/addons/nox/_build/quickjs/build -j
cc -O2 -o kahin/addons/nox/bin/noxhost kahin/addons/nox/noxhost.c \
   -I kahin/addons/nox/_build/quickjs/build -I kahin/addons/nox/_build/quickjs \
   kahin/addons/nox/_build/quickjs/build/libqjs.a -lm -lpthread -ldl
```

Derleme araçları (cmake, cc) yoksa kurulum atlar ve nox üretimi kapalı kalır; WAF'sız
siteler etkilenmez. `KAHIN_SKIP_NOX=1` ile açıkça kapatılır.

## Backshot entegrasyonu

Motor tarafında env gerekmez. Scraper çerezi dosyadan okur:

```
COOKIE=$(jq -r .value "$KAHIN_HOME/nox/gitee.com.cookie")
curl -H "Cookie: nox_jst_v1=$COOKIE" "https://gitee.com/explore/all?order=latest&page=1"
```

Çerez 1188 saniyede bir arka planda yenileniyor. Scraper beklemez; okuduğu değer
aralık içindedir. Aralık dışına düşerse (addon çalışmıyorsa) 405 alır ve yenileme
gerekir — `kahin_waf_cookie_mint(force=true)` ile elle alınabilir.

## Kaynaklar

- `erma0/js-reverse-skill` → `cases/jsvmp-baidu-waf-nox-tox-gitee.md` — `2.0_<hex>_<base64>`
  formatı, `resetNoxJstV1()` manuel tetikleme, `tox_token` ikinci katman
- `LittleSurvival/yamibo-app` → `tools/waf-405-simulator/` — aynı WAF, Android WebView
- `belleangelina/300X` → `waf_challenge_solver.dart` — aynı desen, 30 dk TTL