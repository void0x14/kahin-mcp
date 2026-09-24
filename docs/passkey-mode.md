# Bitwarden passkey modu

## Amaç

Kahin, Firefox tabanlı Camoufox içinde Bitwarden uzantısıyla sitelerin
passkey kayıt ve giriş işlemlerini yapar. Kullanıcı Bitwarden hesabına ilk
kurulumda bir kez kendisi giriş yapar. Kahin daha sonra aynı profili kullanır,
uzantı ayarlarını yapar ve site passkey istemlerini kendi araçlarıyla yönetir.

Bitwarden hesabının **kendisine** Firefox uzantısında passkey ile giriş ayrı
bir özelliktir; [Bitwarden bunu Chromium uzantıları ve web uygulaması için
belgeliyor](https://bitwarden.com/help/login-with-passkeys/). Buradaki özellik,
[Bitwarden kasasındaki passkey'lerle sitelere giriş](https://bitwarden.com/help/storing-passkeys/)
içindir.

## Seçilen yol

1. Mozilla Add-ons'daki imzalı Firefox XPI'si sürüm ve SHA-256 ile doğrulanıp
   yerel önbelleğe indirilir. Profilde `extensions/<Bitwarden-ID>.xpi` olarak
   bulunur. Firefox'un kendi eklenti yöneticisi onu kalıcı eklenti olarak
   yükler; bu, izole profilde iki başlatma boyunca `active=true` ile sınandı.
2. Passkey modu tek bir kalıcı Camoufox profili kullanır. Kahin Juggler ile
   sayfaları ve sekmeleri kontrol eder. Aynı Firefox sürecindeki yerel
   Marionette bağlantısı yalnızca uzantı arayüzüne erişir. İki protokolün
   aynı süreçte birlikte çalışması canlı olarak sınandı.
3. İlk kurulumda görünür Bitwarden sayfası açılır. Kullanıcı hesabına giriş
   yapar; Kahin daha sonra uzantının **Vault timeout = Never** ve
   **Ask to save and use passkeys = On** ayarlarını uzantının kendi arayüzünden
   seçip doğrular. `Never` seçimi sırasında Bitwarden'ın onay penceresi de
   uzantı arayüzü üzerinden tamamlanır. Bu yol, Bitwarden'ın saklama ve
   şifreleme anahtarı yenileme işlemlerini kendi koduyla yürütür.
4. Sonraki başlatmalarda aynı profil ve eklenti kimliği kullanılır. Oturum
   durumu ayrıca kontrol edilir; yalnızca XPI'nin varlığından "kasa açık"
   sonucu çıkarılmaz.

Bitwarden'ın [zaman aşımı belgesine](https://bitwarden.com/help/vault-timeout/)
göre `Never`, kasayı hareketsizlik nedeniyle kilitlemez ve şifreleme anahtarını
cihazda şifresiz tutar. Bu kullanıcı isteğidir. Sunucu tarafında oturumun geri
alınması, hesap değişikliği veya kuruluş politikası gibi dış koşullar için
"ömür boyu tek giriş" garantisi verilemez. Bu koşullarda Kahin yeniden
kimlik doğrulama gerektiğini açıkça raporlar.

## Değerlendirilen yollar

- Camoufox'un çıkarılmış `addons` dizini: Genel eklenti yükleme arayüzü var,
  ancak izole denemede Firefox profilindeki kalıcı eklenti kaydını üretmedi.
  İmzalı XPI'nin profil içindeki kurulumu doğrulanabildiği için seçilmedi.
- Bitwarden masaüstü uygulaması ve native messaging: Bitwarden'ın
  [belgesinde](https://bitwarden.com/help/security-faqs/) native messaging
  biyometrik açma için kullanılıyor. Sitelere passkey sunmak için ek masaüstü
  uygulaması gerekmiyor; bu yol ek bağımlılık getirir.
- Firefox sanal WebAuthn kimlik doğrulayıcısı: Bitwarden kasasındaki kayıtları
  kullanmaz. İstenen Bitwarden entegrasyonunu sağlamaz.

## Doğrulama kapıları

- İmzalı XPI hash'i, manifest kimliği ve sürümü doğru; tekrar başlatmada
  indirme yapılmadan aynı dosya kullanılıyor.
- Firefox `extensions.json` içinde Bitwarden etkin, profil ve uzantı verileri
  temiz kapatıp açmadan sonra korunuyor.
- İlk girişten sonra `Never` ve passkey ayarları uzantı arayüzünde doğrulanıyor;
  ikinci başlatmada da aynı sonuç okunuyor.
- Yerel bir WebAuthn sayfasında passkey kaydetme ve kullanma uçtan uca
  deneniyor. Bitwarden onay penceresi ajan tarafından tamamlanıyor.

Bitwarden passkey uygulaması, sayfanın WebAuthn çağrılarını uzantıdaki
`navigator.credentials.create/get` köprüsüne yönlendirir; [Bitwarden'ın teknik
açıklaması](https://contributing.bitwarden.com/architecture/deep-dives/passkeys/implementations/provider/browser-extension/)
bu mekanizmayı anlatır. Site başına passkey desteği ve kullanıcı doğrulaması
site ile kasa öğesinin koşullarına bağlıdır.
