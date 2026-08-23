# Milestone ve Aktif İş Takibi

**Son güncelleme:** 2026-08-23
**Ana hedef:** `M0.10 — Tam film analizi ve batch çoklu Short üretimi`
**Aktif sıra:** transcript/glossary branch stabilizasyonu → `main` merge → gerçek uzun
filmde batch kabul koşusu → her Short'un ayrı insan onayı
**Ürün yönü:** [ROADMAP.md](ROADMAP.md)
**Ayrıntılı geçmiş:**
[docs/MILESTONES_ARCHIVE_2026-08-23.md](docs/MILESTONES_ARCHIVE_2026-08-23.md)

> Token politikası: Günlük geliştirmede yalnızca bu dosya okunur. Arşiv; eski
> uygulama ayrıntısı, tarihsel test sayısı veya karar kanıtı özellikle gerektiğinde
> hedefli aranır. Arşivin tamamı varsayılan bağlama alınmaz.

## 1. Takip kuralları

| Durum | Anlamı |
|---|---|
| `not_started` | Uygulama başlamadı. |
| `in_progress` | Uygulama veya kabul çalışması sürüyor. |
| `verification_required` | Kod hazır; gerçek ortam, medya, API veya insan kabulü bekliyor. |
| `blocked` | Dış karar, erişim, kota veya bağımlılık gerekiyor. |
| `complete` | Zorunlu kriterler otomatik ve gerçek ortam kanıtıyla kapandı. |
| `deferred` | Bilinçli olarak sonraya bırakıldı. |

Bir milestone ancak zorunlu testleri geçtiğinde, gerçek ortam yolu çalıştığında,
üretilen medya insan tarafından incelendiğinde ve hata/geri dönüş davranışı
kanıtlandığında `complete` olur. Mock test tek başına üretim veya kalite kanıtı değildir.

Her revizyonda yalnızca şunlar güncellenir:

1. Etkilenen durum ve açık görevler.
2. Son doğrulama kanıtı.
2. En fazla 10 satırlık `Yakın dönem kanıtı` bölümü; eski satırlar arşive taşınır.

## 2. Değişmez ürün sınırları

- Ana hat: kaynak → analiz → moment → gerekiyorsa script/TTS → 9:16 render → insan onayı.
- Araştırma yan kolu ana üretim girdilerini otomatik değiştiremez.
- Script, ses, plan ve render onayları içerik hash'ine bağlıdır.
- Secret değerleri istemciye, loga veya artefakta yazılmaz.
- Upload otomatik değildir; ilk yükleme yalnız `private`, görünürlük değişimi ayrı onaylıdır.
- Her output slotu ayrı moment, render, inceleme listesi ve onay taşır.

## 3. Genel durum panosu

| ID | Milestone | Durum | Öncelik | Sonraki kapanış kanıtı |
|---|---|---|---|---|
| M0.0 | Planlama ve güvenlik | `in_progress` | P0 | MVP kabul kriterlerinin son onayı |
| M0.05 | Faz 0 kaynak stratejisi | `verification_required` | P0 | Gerçek Colab mod/plan/kapı doğrulaması |
| M0.1 | Tek-video analiz | `verification_required` | P0 | Gerçek momentlerin görsel doğrulaması |
| M0.2 | Moment Explorer | `verification_required` | P1 | Preview izleme ve seçim kanıtı |
| M0.3 | Script Studio | `verification_required` | P0 | Gerçek script kalite kabulü |
| M0.4 | Voice Studio | `verification_required` | P0 | Gerçek TTS dinleme ve onay |
| M0.5 | Uçtan uca Short PoC | `in_progress` | P0 | İzlenmiş final Short ve tam onay zinciri |
| M0.6 | Temel edit/render | `verification_required` | P0 | 1080×1920 görsel kalite kabulü |
| M0.7 | Private upload | `verification_required` | P2 | Gerçek OAuth ile private upload E2E |
| M0.8 | Analytics | `not_started` | P2 | Gerçek yayın sonrası metrik toplama |
| M0.9 | Creator Console | `verification_required` | P1 | Responsive/erişilebilirlik kabulü |
| M0.10 | Tam film + batch Short | `verification_required` | P0 | Gerçek uzun film ve ayrı Short onayları |
| M0.11 | Montaj motoru | `in_progress` | P1 | Sesli, müzikli, izlenmiş çoklu segment edit |
| M0.12 | Konsol sadeleştirme | `not_started` | P1 | Tek ekranda sade batch/montaj akışı |
| M0.13 | Long-form Studio | `verification_required` | P1 | Gerçek 15–20 dk pilot ve insan izlemesi |
| R0.1 | YouTube araştırma | `verification_required` | P2 | Gerçek keşif ve CSV incelemesi |
| R0.2 | Viral potansiyel | `in_progress` | P2 | Gerçek veride holdout/kalibrasyon |
| M1.0 | Çoklu kanal | `not_started` | P3 | Kanal izolasyon testleri |
| M1.1 | Web/mobil beta | `not_started` | P3 | Mobil onay ve izleme betası |
| M2.0 | SaaS ve ölçek | `deferred` | P4 | MVP sonrası |

## 4. Açık görevler — tek gerçek kaynak

Bu bölüm bütün açık checkbox'ların kanonik yeridir. Aynı görev başka bölümde
tekrarlanmaz.

### P0 — Transcript/glossary branch stabilizasyonu

- [x] Yeniden transkripsiyon manuel segment revizyonlarını sessizce silmemeli; eski
  sürüm arşivlenmeli veya açık kullanıcı kararı istenmeli.
  Elle düzenleme varsa `run_transcript` önceki artefaktı `revisions/<id>/` altına
  arşivliyor, yeni artefakta `replaced_manual_edit` (hangi segmentler, hangi arşiv,
  önceki hash) yazıyor ve `transcript.manual_edits_archived` audit olayı düşüyor.
  Düzenleme yoksa hiçbir tören yapılmıyor. `verify_transcript_editor.py` **113/113**.
- [x] Elle düzeltilmiş `probability=None` kelimeler düşük güvenli veya `0.0` sayılmamalı.
  `transcript_edit.build_segments` ve `render_utils.build_moment_transcript_artifact`
  artık `None`'ı hem ortalamadan hem düşük-güven listesinden dışlıyor;
  `verify_transcript_editor.py` **113/113** (karışık segment dahil yeni kontroller).
- [x] Transcript, tam metin ve glossary taslakları proje/output slot kimliğiyle izole edilmeli.
  Üç taslak da `draftOwner(projectId, slot)` anahtarına bağlandı; sahiplik değişince
  taslak temizleniyor (`longFormDraft`'ın zaten yaptığı korumanın aynısı). Segment
  kimlikleri projeler arasında tekrar ettiği için kaydedilmemiş bir taslak, geçilen
  projenin aynı kimliğine oturup **başka projenin metnini** yazabiliyordu.
  `verify_transcript_editor.py` **113/113**; koruma kapatılıp negatif kontrol
  çalıştırıldı: üç kontrol de düşüyor, yani test boş değil.
- [x] Eski `/transcript/corrections` yolu v2 `asr`, `revisions`,
  `edited_segment_ids` ve `glossary_context` alanlarını korumalı veya kaldırılmalı.
  Yol korundu ve dört alanı da taşıyor; ayrıca `kind="corrections"` revizyonu
  düşüyor. Eskiden `asr`'yi geçirmediği için builder anlık görüntüyü **düzeltilmiş**
  kelimelerden yeniden kuruyordu: "ham ASR'yi geri yükle" düzeltmelerin kendisini
  döndürüyordu. `verify_transcript_editor.py` **113/113**; negatif kontrolde
  alanlar çıkarılınca 3 kontrol düşüyor.
- [x] `Steelers Rider! → Sheepstealer's rider` tam ifade sözlük önerisi üretmeli.
  Segment düzenlemesinde **bitişik** değişen kelimeler artık tek isim sayılıyor
  (`_contiguous_runs`); eskiden kelime-kelime önerildiği için yalnızca
  `Sheepstealer's` kalıyor, `rider` sıradan küçük harfli kelime diye düşüyordu.
  `verify_transcript_editor.py` **113/113**; kontrol artık hangi terimin
  önerildiğini iddia ediyor, sadece "bir öneri var" demiyor.
- [x] `verify_real_preview.py` tam süpürmede düşüyordu; izole koşuda geçiyordu.
  Neden: disk doluluğu / `.console_data` birikmesi. Temizlik sonrası (106 GB boş
  alan) test **REAL_PREVIEW_OK** ile tekrar geçti.
- [x] `verify_diagnostics.py` dahil regresyon paketi yeşil olmalı.
  2026-08-23 ölçümü: `verify_diagnostics` 107/114 → **110/110**, `verify_console`
  çökme → **OK**, `verify_faults` çökme → **70/70**, `verify_acceptance` çökme →
  **114/114**, `verify_batch_outputs` çökme → **95/95**, `verify_real_preview`
  FAILED → **OK**, `verify_forms_progress` 110/111 → **113/113**, `verify_browser`
  15 hata → **243/243**, `verify_local.py` üç bayat Faz 0 iddiası →
  **PHASE0_SOURCE_STRATEGY_TESTS_OK**, `verify_transcript_editor` 82/82 → **113/113**.
  Diğer 20 paket bozulmadı. Kök nedenler: (1) otomasyon faz makinesinde transcript
  adımı yoktu, (2) `diagnostics` `__raw__` yerine `__file__` okumuyordu,
  (3) testler transcript kapısı eklenmeden önceki dünyayı iddia ediyordu.
  Temizlik sonrası tam süpürme (canlı paketler hariç 28 paket): **28/28 yeşil**.
- [x] `feature/transcript-editor-revision-flow` gözden geçirilip `main`e merge edilmeli.
  2026-08-23: çalışma ağacı üç commite bölündü, branch `main`e `--no-ff` ile
  conflict'siz merge edildi (`082cbbc`, 73 dosya). Merge sonrası `main` üzerinde
  `verify_transcript_editor` 113/113, `verify_revision_flow` 39/39,
  `verify_diagnostics` 110/110, `verify_console` OK. Push yapılmadı.

### P0 — M0.10 gerçek medya kabul koşusu

- [ ] Bir saatten uzun gerçek kaynakta iki geçişli tam tarama çalıştırılmalı.
- [ ] En az üç farklı moment üç ayrı output slotuna bilinçli seçilmeli.
- [ ] Her slot transcript'i ayrı okunmalı/düzeltilmeli ve hash ile onaylanmalı.
- [ ] Üç batch preview baştan sona izlenmeli; en az bir kabul ve bir ret/revizyon kaydedilmeli.
- [ ] En az bir Short 1080×1920 final kalitede render edilip teknik/görsel kontrolden geçmeli.
- [ ] Kadraj, kesim, altyazı senkronu, aktif kelime vurgusu ve ses sürekliliği
  gözlemleri kanıt paketine yazılmalı.

### M0.0 — Planlama ve güvenlik

- [ ] MVP PRD ve kabul kriterlerinin kullanıcı son onayı.
- [ ] Hedef Short türlerinin önceliklendirilmesi: recap, theory, scene explanation vb.
- [ ] Telif ve dönüştürücü kullanım kontrol listesinin ürün akışına eklenmesi.
- [ ] Private upload yetkilendirme sınırının tanımlanması.

### M0.05 — Faz 0 kaynak stratejisi

- [ ] Gerçek Colab koşusunda Faz 0 paneli okunup mod bilinçli seçilmeli.
- [ ] `processing_plan.json` gerçek hash ile onaylanıp alt fazların açıldığı gözlenmeli.
- [ ] `direct_clip_series` gerçek koşuda sıfır Gemini/TTS çağrısı yapmalı.
- [ ] Kaynak değişince onayın düştüğü canlı ortamda gözlenmeli.
- [ ] Çoklu çıktı planında ek slotların ayrı dizinlerde raporlandığı görülmeli.

### M0.1 — Tek-video analiz

- [ ] Analizin videonun amaçlanan bölümünü kapsadığı doğrulanmalı.
- [ ] Aday moment başlangıç/bitiş zamanları görüntüyle doğrulanmalı.
- [ ] En az bir kabul ve bir ret kararı kaydedilmeli.

### M0.2 — Moment Explorer

- [ ] Gerçek Colab videosunda bütün preview'lar baştan sona izlenmeli.
- [ ] Favori kararı ayrı kaydedilmeli.
- [ ] Birden fazla moment seçme ve sıralama tamamlanmalı.
- [ ] Önceki/sonraki bağlam genişletme eklenmeli.
- [ ] Karakter, duygu, aksiyon, diyalog ve spoiler filtreleri eklenmeli.

### M0.3 — Script Studio

- [ ] Gerçek `GeminiKey` ile başarılı script üretimi doğrulanmalı.
- [ ] İlk 2 saniyelik hook kalite kontrolü yapılmalı.
- [ ] Hedef süre ile gerçek konuşma uzunluğu karşılaştırılmalı.
- [ ] Viral, analitik ve sinematik varyantlar tamamlanmalı.
- [ ] Cümle bazında kaynak moment/evidence bağlantısı kurulmalı.
- [ ] Theory Guard v0 tamamlanmalı.

### M0.4 — Voice Studio

- [ ] Gerçek Gemini TTS çağrısı başarılı olmalı.
- [ ] WAV baştan sona dinlenmeli.
- [ ] Türkçe telaffuz kabul edilmeli.
- [ ] Hız, ton, duraklama ve ifade hedef içeriğe uygun bulunmalı.
- [ ] Ses süresi hedef Short süresiyle uyumlu olmalı.
- [ ] En az bir gerçek başarısızlık/fallback davranışı gözlenmeli.

### M0.5 — Uçtan uca Short PoC

- [ ] İsteğe bağlı `narrated_recap`: Gemini script üretimi ve insan onayı doğrulanmalı.
- [ ] İsteğe bağlı `narrated_recap`: Gemini TTS ve ses onayı doğrulanmalı.
- [ ] Final render 1080×1920 üretilip izlenmeli.
- [ ] Altyazı okunabilirliği ve konuşma senkronu kabul edilmeli.
- [ ] İsteğe bağlı voice-over anlaşılır ve ses dengesi kabul edilmiş olmalı.
- [ ] Teknik doğrulama video ve ses stream'lerini başarılı bulmalı.
- [ ] Render manifestindeki bütün üst-akış onay koşulları `True` olmalı.
- [ ] `final_short.mp4` hiçbir platforma otomatik yüklenmemiş olmalı.
- [ ] Çalıştırma özeti ve hata dosyaları saklanmalı.

### M0.6 — Edit/render kalitesi

- [ ] Altyazı taşma, font ve zamanlama kontrolü geçmeli.
- [ ] 1080×1920 final render teknik ve görsel kontrolden geçmeli.

### M0.7 — Private upload

- [ ] Yetkilendirilmiş gerçek Google/YouTube OAuth hesabıyla private upload PoC.
- [ ] Videonun YouTube Studio'da gerçekten `private` olduğu gözlenmeli.
- [ ] Kesilen/resume edilen upload gerçek ağda doğrulanmalı.
- [ ] Yanlış/eksik onayla upload ve görünürlük değişimi reddedilmeli.
- [ ] Public/unlisted değişimi yalnız ayrı kullanıcı onayıyla yapılabilmeli.
- [ ] Kota, hata ve geri dönüş kayıtları secret sızdırmadan saklanmalı.

### M0.9 — Creator Console

- [ ] Masaüstü ana akış ve mobil izleme/onay görünümü görsel kontrolden geçmeli.
- [ ] Erişilebilirlik, responsive davranış ve temel performans smoke testleri geçmeli.

### M0.11 — Montaj motoru

- [ ] Kliplerin orijinal sesleri video sırasıyla normalize ve concat edilmeli.
- [ ] Kullanıcının müziği gerçek FFmpeg girdisi olarak bağlanmalı.
- [ ] Müzik + orijinal ses miksinde ortak audio-ducking motoru kullanılmalı.
- [ ] Süre, stream, çözünürlük, fps ve SAR teknik doğrulaması yapılmalı.
- [ ] `music_track`, segmentler ve `audio_mix` kararları onay hash'ine bağlanmalı.
- [ ] Kullanıcı müziği/telif sorumluluğu açık gösterilmeli; sistem müzik indirmemeli/üretmemeli.
- [ ] Karakter farkındalığı v0 transcript isim-geçiş indeksi ve segment etiketiyle yapılmalı;
  yüz tanıma kapsam dışı kalmalı.
- [ ] Script/TTS yapısal olarak kapalı ve sağlayıcı çağrısı sıfır olmalı.
- [ ] Gerçek kaynak ve kullanıcı müziğiyle en az üç segmentli edit render edilip izlenmeli.

### M0.13 — Long-form Studio

- [ ] Outline üretimi, düzenleme ve ayrı insan onayı tamamlanmalı.
- [ ] Bölüm/B-roll eşleme ve 16:9 timeline yürütücüsü tamamlanmalı.
- [ ] Preview/final render ve arşiv aşamaları uçtan uca çalışmalı.
- [ ] Kota/billing açıldıktan sonra gerçek pilot script ve TTS aşamalarına kadar tekrarlanmalı.
- [ ] Üretilen sesin sürekliliği elle dinlenmeli.
- [ ] TTS/kendi sesi seçimi, müzik ducking ve tam video altyazısı tamamlanmalı.
- [ ] 720p preview ve 1080p final render üretilmeli.
- [ ] Gerçek bir konuda 15–20 dakikalık video baştan sona izlenmeli.

### R0.1 — YouTube araştırma

- [ ] Colab'da gerçek anahtar kelime keşfi çalıştırılmalı.
- [ ] `discovered_videos.csv` ve `discovered_channels.csv` insan tarafından incelenmeli.
- [ ] Kota raporu Google Cloud Console ile karşılaştırılmalı.
- [ ] Arama kalitesi için kabul/ret örnekleri kaydedilmeli.

### R0.2 — Viral potansiyel

- [ ] Yeterli gerçek kanal/video geçmişi toplanmalı.
- [ ] Baseline sonuçları gerçek veride değerlendirilmeli.
- [ ] Kanal ortalamasına göre kalibrasyon yapılmalı.
- [ ] Tahmin aralıkları ve güven seviyesi doğrulanmalı.

## 5. Tamamlanan yetenekler — kompakt özet

- Güvenlik, secret izolasyonu, hash-bağlı insan onayı ve otomatik yayın yasağı.
- Faz 0 kaynak değerlendirmesi ve `direct_clip_series` / `narrated_theory` ayrımı.
- Yerel video analizi, transcript, sahne/enerji sinyalleri ve hot moment üretimi.
- Moment preview, seçim/ret kaydı ve seçimin kaynak hash'ine bağlanması.
- Gemini script/TTS rotaları, script/ses inceleme ekranları ve canlı tek çağrı doğrulamaları.
- 9:16 crop/blur, dinamik altyazı, ses miksleme ve FFmpeg teknik kontrolleri.
- Creator Console, kalıcı işler, retry/cancel/recovery, akış haritası ve insan kapıları.
- Fault-injection, tarayıcı, yerel kabul ve gerçek kaynak preview testleri.
- İki geçişli tam kaynak taraması, output-slot izolasyonu ve batch render mekaniği.
- Long-form kalıcı aşamalar, kota/billing blokları ve yeniden başlatılabilir TTS defteri.
- Stok medya adaptörleri, sağlayıcı/secret güvenliği ve telif iddiası yapmayan kayıtlar.
- Turbo Studio aşama grafiği, timeline/FFmpeg motoru, müzik lisans durumu ve arşivleme.

Ayrıntılı uygulama gerekçeleri, hata öyküleri ve eski test sonuçları arşivdedir.

## 6. Güncel doğrulama özeti

| Alan | Son kayıtlı kanıt | Açık insan/gerçek ortam kapısı |
|---|---|---|
| Yerel kabul | `verify_acceptance.py` **114/114** | Final Short insan onayı |
| Tam film/batch | `verify_batch_outputs.py` **95/95** | >1 saat gerçek kaynak ve üç Short izleme |
| Tarayıcı UI | `verify_browser.py` **243/243** | Mobil/erişilebilirlik gözlemi |
| Canlı Gemini script | `LIVE_SCRIPT_OK`, tek gerçek çağrı | Script kalite yargısı |
| Canlı Gemini TTS | `LIVE_TTS_OK`, tek gerçek çağrı | WAV/voice karşılaştırmalı dinleme |
| Long-form | `verify_long_form_studio.py` **97/97** | Billing/kota sonrası gerçek 15–20 dk pilot |
| Stok medya | `verify_stock_media.py` **41/41** (Pexels, Pixabay, Coverr) | Gerçek sağlayıcı araması; **Coverr şeması canlı doğrulanmadı** |
| Turbo Studio | `verify_turbo_studio.py` **333/333**, tarayıcı **24/24** | Gerçek sağlayıcılar ve izlenmiş final video |

## 7. Bir sonraki çalışma oturumu

1. `main` `origin/main`e push edildi (`a53b751`); bu madde kapalı.
2. M0.10 (MoneyPrinter/Turbo yetenek farkı): kod tarafı 2026-08-23'te kapandı.
   - Dinamik ASS altyazı (aktif kelime vurgusu) + caption teması; `subtitle_style`
     revizyonu artık temayı değiştirip ASS'i yeniden üretiyor.
   - Coverr üçüncü stok sağlayıcı olarak eklendi.
   - Görsel klip + Ken Burns: `timeline` klip türü öğrendi, gerçek karışık
     (video+görsel) ffmpeg render'ı doğrulandı.
   - Turbo script üretimi Google tükendiğinde Groq/Cloudflare zincirine geçiyor;
     güvenlik reddi taşınmıyor.
   - **Açık:** görsel stok ARAMA uçları yok (motor hazır, besleme hattı değil);
     Coverr şeması canlı bir çağrıyla doğrulanmadı.
3. Creator Console'da >1 saat gerçek kaynakla final kalite planı oluştur.
4. Tam kaynak taraması çalıştır; üç ayrı momenti üç slota seç.
5. Her slot transcript'ini ayrı incele/onayla ve batch renderı çalıştır.
6. Üç Short'u ayrı izle; kadraj, kesim, altyazı, vurgu, font ve sesi kaydet.
7. En az bir kabul ve bir ret/revizyon kararı ver; gözlenen kanıta göre checkbox'ları güncelle.
8. `narrated_theory` denenirse script okunmalı, üretilen bütün WAV'lar dinlenmelidir.

## 8. Yakın dönem kanıtı

Bu bölüm en fazla 10 kısa satır tutulur. Ayrıntı ve eski kayıtlar arşive taşınır.

- 2026-08-23 — Turbo yetenek farkı kapandı: dinamik ASS, caption teması, Coverr,
  görsel klip + Ken Burns, çapraz sağlayıcı script zinciri. `verify_turbo_studio`
  260 → **333/333**, `verify_stock_media` 29 → **41/41**; tam süpürme **28/28**.
- 2026-08-23 — Ken Burns hareket testi ilk yazımda sahteydi (kodlanmış kareleri
  karşılaştırıyordu, hareket kapalıyken de geçiyordu); ham rgb24 piksel ölçümüne
  çevrildi: pan varken ~95, yokken 0.002 ortalama mutlak fark.
- 2026-08-23 — Otomasyon `direct_clip_series`'te kilitleniyordu: faz makinesinde
  transcript adımı yoktu, render kapıda reddedilip `phase_attempt_limit` ile ölüyordu.
  `transcript` makine fazı + `transcript_approval` insan kapısı eklendi.
- 2026-08-23 — `diagnostics` path-traversal canary kontrolü `__raw__` okuduğu için
  hiçbir içeriği incelemiyordu (API `__file__`'a geçmişti); sessizce her zaman geçiyordu.
- 2026-08-23 — Elle düzeltilen kelime `probability=None` alıyor ama iki tüketici bunu
  `0.0` sayıyordu; isim düzeltmek transcript'i daha kötü gösteriyordu. Düzeltildi.
- 2026-08-23 — Disk temizliği (106 GB boş) sonrası `verify_real_preview` tekrar
  **REAL_PREVIEW_OK**; tam süpürme (canlı paketler hariç 28 paket) **28/28 yeşil**.
- 2026-08-22 — Stok medya adaptörü: mock sağlayıcı ve güvenlik testleri geçti; canlı kabul bekliyor.
- 2026-08-22 — Turbo Studio: aşama grafiği ve gerçek FFmpeg mekaniği geçti; insan izlemesi yok.
- 2026-08-23 — P0 transcript/glossary stabilizasyon testleri yeniden ölçüldü:
  `verify_transcript_editor` 113/113, `verify_revision_flow` 39/39,
  `verify_diagnostics` 110/110; ilgili 10 regresyon paketi tamamen yeşil.
- 2026-08-23 — `main` `origin`e push edildi (`a53b751`); MoneyPrinter/Turbo fark
  kapatma oturumu başladı. Edge-TTS (kimliksiz fallback), ses hızı normalizasyonu
  ve WAV chunk yürütme hatası çözüldü (`914f133`). ASS aktif-kelime vurgusu
  Turbo'ya bağlandı (henüz commitlenmedi/test edilmedi).
