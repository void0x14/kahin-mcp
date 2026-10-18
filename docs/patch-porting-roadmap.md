# Otonom Patch Taşıma — Yol Haritası

## Amaç

Bir upstream sürümü değiştiğinde, mevcut patch setini **sıfır eforla** ve **ultra hızlı** şekilde yeni sürüme taşımak. Taşıma, katı semantik kapılardan geçmeden tamamlanmış sayılmaz; kapıyı geçemeyen yükseltme **reddedilir ve yükseltme yapılmaz**.

## Değişmez kurallar

- Motor **yapay zeka** olmak zorundadır. LLM'siz, kural tabanlı, elle rebase akışı bu roadmap'in çözümü değildir.
- Modeller **opencodex proxy** üzerinden gelir.
- Görev için özel bir **combo** oluşturulur: fonksiyonelite, regresyon ve anomali tespitinde en iyi olan modeller bu comboda toplanır.
- Combo **round-robin** stratejisiyle çalıştırılır; her model aynı işi bağımsız üretir, sonuçlar kapılarda yarıştırılır.
- Taşıma sonucu, mevcut çalışan sürümle **A/B** karşılaştırmasına girer.

## A/B doğrulama

- **A tarafı:** mevcut sürümün, tarafımızca test edilip **dondurulmuş** veri setleri.
- **B tarafı:** taşıma sonrası sistemin kendi ürettiği testler.
- A ve B sonuçları **%100 eşit olmak zorundadır**.
- En kötü ihtimalle **%95 eşitlik** kabul edilir.
- %95'in altındaki her sonuç **reddedilir**; yükseltme yapılmaz, mevcut sürüm korunur.

## Semantik kapılar

Her taşıma şu üç kapıdan geçer:

1. **Fonksiyonelite:** %100 korunur. Hiçbir davranış kaybı kabul edilmez.
2. **Regresyon:** %0. A tarafında çalışan hiçbir şey B tarafında bozulmaz.
3. **Anomalibility:** %0. Taşıma sonrası yeni bir anomali, yeni bir hata yüzeyi veya yeni bir belirsizlik oluşmaz.

Kapıların görevi, fonksiyoneliteyi %100 koruyarak, %0 regresyon ve %0 anomalibility ile güncellemeyi tamamlamaktır. Kapı, "derlendi" veya "patch uygulandı" demek değildir; davranışın kanıtlanmasıdır.

## AI motoru

- Model seçimi görev bazlıdır: hunk yeniden yazımı, semantik eşleme, test üretimi ve regresyon analizi için ayrı yetenek profilleri.
- Combo, opencodex proxy üzerinden tek arayüzle beslenir.
- Round-robin: aynı taşıma işi birden fazla modele dağıtılır; en iyi sonuç kapılardan geçen sonuçtur.
- Hiçbir model tek başına "onaylayıcı" değildir; onay yalnızca A/B + semantik kapılardan gelir.

## Git

- Mevcut git'ten nefret ediliyor.
- Hedef: **Elon Musk'ın yaptığı, sıfır çakışma üreten yeni git**. Taşıma sistemi bu git üzerine kurulacak.
- Mevcut git ile çakışma çözme, "hunk elle düzeltme" veya `rerere` benzeri tekrar çözüm mekanizmaları bu roadmap'in hedef çözümü değildir; yalnızca geçiş dönemi aracı olarak not edilir.

## Referans sistemler (girdi, çözüm değil)

Araştırmada bulunan ve tasarıma girdi olabilecek sistemler:

- **PortGPT** — hunk bazlı LLM ajanı; Git geçmişi, sembol konumu ve derleyici geri bildirimi ile iteratif taşıma.
- **VeriPort** — deterministik uygulamadan semantik adaptasyona "apply ladder"; iki taraflı doğrulama kapısı; sürüm zincirleri.
- **FixMorph** — LLM'siz kural sentezi (referans; hedef motor değil).
- **LLMPort** — görev bölme + test geri bildirimi ile dosyalar arası taşıma.
- **rebase-helper / git-buildpackage pq / git rerere** — mevcut git dünyasının otomasyonları (geçiş dönemi referansı).

Hiçbiri "sıfır efor + ultra hızlı + %100 semantik kapı" hedefini tek başına karşılamıyor; bu roadmap onların üzerine kendi motorunu ve kendi kapılarını koyar.

## Açık maddeler

- [ ] opencodex proxy üzerinde görev bazlı model combosunun tanımlanması.
- [ ] Round-robin dağıtım ve sonuç seçim kriterlerinin tanımlanması.
- [ ] A tarafı dondurulmuş veri setlerinin envanteri ve sürüm damgası.
- [ ] B tarafı test üretiminin kapsamı (fonksiyonelite + regresyon + anomali).
- [ ] %100 / %95 eşiklerinin ölçüm yöntemi ve rapor formatı.
- [ ] Reddedilen yükseltmenin geri alma (rollback) sözleşmesi.
- [ ] Sıfır çakışmalı yeni git entegrasyonu.
