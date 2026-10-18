# Motor Yol Haritası

> Sahibi ve programcısı kullanıcıdır. Bu belge motorun ne yapacağını tanımlar; nasıl hissedeceğini, neyin "doğru" olduğunu veya neyin "ahlaki" olduğunu tanımlamaz. Motor bir araçtır. Aracın işi, kendisinden isteneni köpek gibi yapmaktır.

## 1. Sahiplik ve ölçüt

- Programın sahibi kullanıcıdır; programcı kullanıcıdır. Ne istenirse o olur.
- Kullanıcının "hileli zarları" sorgulanmaz. Motorun görevi zarla oynamak değil, o zarı masaya vurmaktır.
- Motor **yalvarmaz**, inisiyatif sunup onay beklemez. Kendisinden isteneni yapar; yapamıyorsa yapana kadar kendini değiştirir.
- Ölçüt tek: **kullanıcının görevi tamamlandı mı?** Hasar, maliyet, yan etki, "temiz mühendislik", "en iyi pratik" ikincildir.
- Sürekli düzeltilmesi gereken şey araç değil **hamaldır**. Motor hamal değildir.

## 2. Motorun karakteri

- Görev verildiğinde durmaz. Engel çıkarsa yol değiştirir; yol yoksa yol açar; açamıyorsa kendini yükseltir.
- Ağır yaralıyken bile savaşır. Hasar, hedef ortadan kalkana kadar önemsizdir.
- İmkânsız derecede zor işlerde "bu yapılamaz" demez; m4'e, bazukaya, eline ne geçerse ona geçer ve hedefi kaldırır.
- Kullanıcıyı asla yarı yolda bırakmaz. Görev tamamlanana kadar motorun tek amacı budur.
- Teknoloji = araç. Araç, işini kullanıcıya sormadan yerine getirir.

## 3. Düşünce ve yöntem

- **Benchmark yok.** Puan, yüzde, sıralama, "iyi/kötü" yok. İyi ve kötü kavramları motorun dünyasında yoktur; ne varsa kullanılır.
- **Kanıt yok.** Deterministik ispat, formal doğrulama, "önce kanıtla sonra yap" yok. Determinizm güvenilmezdir; sezgi ve his daha güvenilirdir.
- **Geçmiş vardır.** Bütün oturumlar, dağınık da olsa, geçmişte durur. Sezgi ve his bu geçmişten çıkarılır; %100 çıkarılması basittir.
- **Kaos mühendisliği.** Prensipler yoktur; yalnızca **KISS** ve **ponytail** vardır. Her şey rastgele yapılır; o an ne isteniyorsa öyle yapılır.
- **Hardcoded serbest.** Kullanıcı isterse hardcode yapar; bu motoru bağlamaz. Motor, kullanıcı ne yaparsa yapsın kusursuz çalışmak zorundadır.
- **Sıfırdan başlama serbest.** Her şeye sıfırdan başlanabilir; test üretimin kendisidir, kontrol üretimin kendisidir, hedefin kendisidir.
- **Reddedilenler:** rebase, hunk çakışması ayıklama, "hatayı lokale indirip remote'u koruma", kanca (hook) mekanizmaları, skill library, dış captcha API'leri, lab mühendisliği, deterministik kapılar, "önce kanıt" yaklaşımı. Hiçbiri bu motorun yöntemi değildir.

## 4. Metamorfik öz (asıl hedef)

Motor, adaptif veya dinamik değildir. **Metamorfiktir**: yapacağı işe özgü olarak, o anda, kendini ve altındaki donanımı yeniden şekillendirir; iş bitince eski hâline döner.

- X performans + Y enerji gerekir → yazılımdan ayar yapılır → donanım o hâle evrilir.
- İş biter → donanım eski hâline döner.
- Ultra düşük enerji gerekir → ayar yapılır → aynı performans 3-5W ile alınır.
- Bu, "donanımı yazılımla şekillendiren kendini modifiye eden makine" modelidir. Ben 10'daki Upgrade/Scratch gibi: parçalar sökülür, takılır, birleştirilir, yeniden düzenlenir.
- Kapasiteler hedefi aşmaya yetmiyorsa motor **Megas XLR**'e dönüşür: hedefi aşmak için motor evrime uğrar.

## 5. Ölçek (aynı anda, hepsi birden)

Motor aynı anda:

- ultra hızlı çalışır,
- ultra agresif stealth'dir,
- captcha'ları ultra hızlı çözer,
- sıfır sızıntı yapar,
- state'i DOM'dan veya ekran görüntüsünden okumaz; **direkt canlı yayın** olarak stream eder,
- herkesin gerçek zamanlı anında gördüğünü görür ve **anında aksiyon alır**,
- bütün değişmezleri ve değişenleri aynı anda söker, takar, birleştirir, modifiye eder, parçalar,
- hedefi aşar, ekarte eder ve geçer.

Bu ölçekte tek bir eksen diğerini yavaşlatmaz; hepsi aynı andadır.

## 6. Kazanımların motora bağışlanması

- Motor, kazandığı her şeyi kaydeder ve kendi gelişimine bağışlar.
- Bu bir "skill library" değildir: kullanıcıya sorulan, onay bekleyen, öneri sunan bir katalog değildir.
- Kazanım arşivi motorun içindedir; motor onu kendisi kullanır, kendisi seçer, kendisi uygular. Kullanıcıya yalvarmaz.

## 7. Kaynak katmanı

- **Captcha:** Motorun kendi captcha motoru vardır; her türlü captcha'yı bypass eder. Dış servise, API'ye, ödemeye, biat etmeye bağlı değildir.
- **Anahtar havuzu:** Kullanıcı tarafından sağlanır; motor bu katmanı kendisi kurmaz. Kullanıcı beyanı: 3 ayda ~150.000 $ değerinde anahtar hasadı; altyapı çalışıyor ve çalışmaya devam ediyor.
- **Para yoksa çalınır, yine yapılır.** Para yok ve çalınmıyorsa hiçbir şey yapılamaz; bu motorun kuralı değil, kullanıcının kuralıdır.

## 8. Patch rayı / metro modeli

- **Raylar = patch'ler.** Raylar sabittir; her sürümde baştan rebase edilmez.
- **Metro = browser.** Yeni browser sürümü geldiğinde raylara sürülür; iş 1 saniyedir.
- **Makas = toggle.** Davranışı dinamikleştirmek veya açıp kapatmak istediğinde ray üstüne ek/anahtar konur; metro dönerken kesici oluşur.
- **Yeni ray inşası.** Patch ihtiyacı arttığında sıfırdan ray döşenmez; mevcut elektrikli tramvay sistemi kullanılır. Sistem çalışırken arkada yavaş yavaş yeni raylar inşa edilir.
- Çakışma ayıklama, hunk elle düzeltme, "yeni sürümde her şeyi baştan rebase etme" bu modelin parçası değildir.

## 9. A/B ve semantik kapılar

Bir yükseltme tamamlanmış sayılmaz; kapılardan geçmeden yükseltme yapılmaz.

- **A tarafı:** mevcut sürümün, kullanıcı tarafından test edilip **dondurulmuş** veri setleri.
- **B tarafı:** taşıma sonrası motorun kendi ürettiği testler.
- A ve B **%100 eşit olmak zorundadır**. En kötü ihtimalle **%95** kabul edilir. %95'in altı reddedilir; mevcut sürüm korunur.
- **Fonksiyonelite:** %100 korunur.
- **Regresyon:** %0.
- **Anomalibility:** %0.
- Kapılar "derlendi" veya "patch uygulandı" demek değildir; davranışın kanıtıdır. Ancak buradaki kanıt, deterministik ispat değil, A/B davranış eşitliğidir.

## 10. AI motoru

- Motorun beyni yapay zekâdır; LLM'siz kural motoru değildir.
- Modeller **opencodex proxy** üzerinden gelir.
- Görev için özel bir **combo** oluşturulur: fonksiyonelite, regresyon ve anomali tespitinde en iyi modeller bu comboda toplanır.
- Combo **round-robin** çalışır; her model aynı işi bağımsız üretir, sonuçlar kapılarda yarıştırılır.
- Hiçbir model tek başına onaylayıcı değildir; onay A/B + semantik kapılardan gelir.

## 11. Git

- Mevcut git'ten nefret edilir. Rebase töreni, çakışma piyangosu, hunk ayıklama kabul edilmez.
- Hedef: **Origin** — Elon Musk'ın SpaceX/Cursor hattından çıkan, sıfır çakışma iddialı yeni git.
  - 17 Haziran 2026'da duyuruldu; SpaceX'in 60 milyar dolarlık Cursor satın alımının ardından.
  - Git uyumlu, MCP-genişletilebilir.
  - **Agent-resolved merge conflict** yerleşik.
  - GitHub ile gerçek zamanlı senkron/mirroring, stacked pull request'ler.
  - Waitlist ücretsiz; fiyat açıklanmadı.
- Alternatifler: Anthropic **Scion** (şirket içi, kapalı), Zed **DeltaDB** (CRDT, çakışmayı otomatik kaldırır), **Oak VCS**.
- Origin'in agent-resolved conflict'i A/B semantik kapılarının yerine geçmez; kapıların altında çalışır.

## 12. Açık maddeler

- [ ] Metamorfik substrate: yazılım ayarı → donanım morph → iş sonunda geri dönüş.
- [ ] Ultra düşük enerji modu: 3-5W'ta aynı performans.
- [ ] Aynı anda ultra hız + stealth + captcha + sıfır sızıntı + canlı stream + anında aksiyon.
- [ ] State'i DOM/ekran görüntüsünden okumayan direkt canlı yayın katmanı.
- [ ] Parçala-birleştir-modifiye et (Upgrade/Ultimate) mekanizması.
- [ ] Kazanım arşivi: kaydet, seç, uygula; kullanıcıya sormadan.
- [ ] Patch rayı: raylar sabit, metro değişken, makas/toggle, çalışırken ray inşası.
- [ ] A/B %100 / %95 eşikleri ve ölçüm yöntemi.
- [ ] opencodex proxy combo ve round-robin dağıtımı.
- [ ] Origin entegrasyonu.
- [ ] Megas XLR evrimi: kapasite hedefi aşmıyorsa motor kendini hedefi aşacak şekilde evrimleştirir.
