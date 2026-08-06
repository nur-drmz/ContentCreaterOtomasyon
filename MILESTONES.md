# Ayrıntılı Milestone ve İlerleme Takibi

**Son güncelleme:** 2026-08-03  
**Ürün hedefi:** Kullanıcının verdiği uzun videodan seçilen anı, orijinal sesi
koruyup zaman kodlu altyazı ekleyerek izlenebilir 9:16 bir YouTube Short'a çevirmek.
Gemini script + TTS anlatımı isteğe bağlı ikinci moddur.  
**Şu anki ana hedef:** `M0.5 — Gerçek Colab uçtan uca sesli Short PoC`
**Sıradaki doğrulama:** `M0.5 — Gerçek Colab'da Faz 0 stratejisi seçilip onaylanarak uçtan uca koşu`

Bu belge günlük ilerleme ve kabul takibi içindir. Üst seviye ürün yönü ve özgün
release fazları için [ROADMAP.md](ROADMAP.md) esas alınır.

## 1. Takip kuralları

### Durum sözlüğü

| Durum | Anlamı |
|---|---|
| `not_started` | Uygulama başlamadı. |
| `in_progress` | Çalışma başladı ancak kabul kriterlerinin tamamı karşılanmadı. |
| `verification_required` | Kod hazır; gerçek ortam, gerçek medya veya gerçek API ile kanıt bekleniyor. |
| `blocked` | İlerleme için dış karar, erişim veya eksik bağımlılık gerekiyor. |
| `complete` | Tüm zorunlu kabul kriterleri kanıtlarıyla karşılandı. |
| `deferred` | MVP dışına bilinçli olarak ertelendi. |

### Tamamlandı sayma politikası

Bir milestone yalnızca aşağıdakilerin tamamı sağlandığında `complete` olabilir:

- Zorunlu kabul kriterleri karşılanmış olmalı.
- İlgili otomatik testler geçmeli.
- Gerçek ortam gerektiren yol en az bir kez gerçek veri/kimlik bilgisiyle çalışmalı.
- Üretilen medya insan tarafından izlenmiş veya dinlenmiş olmalı.
- Hata ve geri dönüş davranışı gözlemlenmiş olmalı.
- Kanıt dosyaları bu belgede belirtilmiş olmalı.

Mock testleri kod sözleşmesini kanıtlar; gerçek API erişimini veya üretim
kalitesini tek başına kanıtlamaz.

### Zorunlu revizyon kayıt protokolü

Bu projede yapılan **her revizyon**, aynı çalışma içinde bu dosyaya da
yansıtılmalıdır. Kod değişip milestone kaydı değişmeden revizyon tamamlanmış
sayılmaz.

Her revizyondan sonra zorunlu adımlar:

1. Üstteki `Son güncelleme` tarihi kontrol edilir.
2. Etkilenen milestone'un durumu gözden geçirilir.
3. Tamamlanan veya açılan kabul kriterleri işaretlenir.
4. Çalıştırılan test ve doğrulama kanıtı güncellenir.
5. `İlerleme günlüğü` tablosuna tarihli bir satır eklenir.
6. Canlı ortamda denenmeyen iş `complete` yapılmaz; en fazla
   `verification_required` olur.

İlerleme günlüğü satır şablonu:

```text
| YYYY-MM-DD | Yapılan revizyon | Mx.y | Dosya/test/artefakt kanıtı |
```

## 2. Mimari sınır

```text
ANA ÜRETİM HATTI
Kaynak video
  → yerel analiz
  → hot moment seçimi
  → script üretimi ve onayı
  → seslendirme ve onayı
  → 9:16 render ve onayı
  → final_short.mp4

BAĞIMSIZ ARAŞTIRMA YAN KOLU
Anahtar kelime
  → YouTube trend/kanal keşfi
  → veri seti
  → viral potansiyel deneyi
```

Araştırma yan kolu kaynak videoyu, hot moment'i, script'i, sesi veya render
girdisini otomatik değiştiremez. Araştırma artefaktları
`pipeline_role="research_sidecar"` ve `feeds_main_pipeline=false` taşır.

## 3. Genel durum panosu

| ID | Milestone | Durum | Öncelik | Sonraki kapanış kanıtı |
|---|---|---|---|---|
| M0.0 | Planlama ve güvenlik ilkeleri | `in_progress` | P0 | MVP kabul kriterlerinin son onayı |
| M0.1 | Tek-video analiz prototipi | `verification_required` | P0 | Gerçek videoda Colab analiz çıktısı ve moment incelemesi |
| M0.2 | Moment Explorer v0 | `verification_required` | P1 | Gerçek Colab videosunda preview izleme ve seçim kanıtı |
| M0.3 | Prompt ve Script Studio v0 | `verification_required` | P0 | Gerçek Gemini çağrısı, okunmuş ve hash-onaylı script |
| M0.4 | Voice Studio v0 | `verification_required` | P0 | Gerçek Gemini TTS WAV, dinleme ve hash onayı |
| M0.05 | Faz 0 kaynak işleme stratejisi | `verification_required` | P0 | Gerçek Colab'da mod seçimi, plan onayı ve kapıların gözlenmesi |
| M0.5 | Uçtan uca sesli Short PoC | `in_progress` | P0 | İzlenmiş `final_short.mp4` ve tam artefakt zinciri |
| M0.6 | Temel edit/render kalitesi | `verification_required` | P0 | Gerçek içerikte 1080×1920 final kalite kontrolü |
| R0.1 | YouTube araştırma yan kolu | `verification_required` | P2 | Gerçek anahtar-kelime keşfi ve CSV incelemesi |
| R0.2 | Viral potansiyel araştırması | `in_progress` | P2 | Yeterli gerçek tarihçe üzerinde holdout raporu |
| M0.7 | Private upload ve yayın güvenliği | `not_started` | P2 | Ayrı yetkilendirilmiş private-upload PoC |
| M0.8 | Analytics ve öğrenme | `not_started` | P2 | Gerçek yayın sonrası temel metrik toplama |
| M0.9 | Creator Console pre-alpha | `verification_required` | P1 | Gerçek kaynak videoyla arayüzden preview Short ve mobil inceleme kontrolü |
| M1.0 | Çoklu kanal ve Channel Brain | `not_started` | P3 | Kanal bazlı ayar ve izolasyon testleri |
| M1.1 | Web/mobil beta | `not_started` | P3 | Onay ve izleme iş akışlı beta |
| M2.0 | SaaS ve ölçek | `deferred` | P4 | MVP sonrasına ertelendi |

## 4. Aktif milestone — M0.5 Uçtan uca sesli Short PoC

**Durum:** `in_progress`  
**Amaç:** Tek bir gerçek kaynak videodan, insan tarafından seçilmiş momenti
orijinal sesi ve zaman kodlu dinamik altyazısıyla izlenebilir bir Short'a çevirmek.  
**Ana çalışma dosyası:**
[experiments/colab/00_youtube_automation_master.ipynb](experiments/colab/00_youtube_automation_master.ipynb)

### Çalıştırma sırası

1. Kurulum, Secrets, Drive ve gömülü modül bootstrap hücreleri.
2. **M0.05 Faz 0 — kaynak değerlendirmesi, mod seçimi ve işleme planı onayı (ZORUNLU).**
3. M0.1 video analizi.
4. M0.2 moment inceleme ve seçim.
5. Ana modda seçilen klibin orijinal sesini Whisper ile zaman kodlu altyazıya çevirme.
6. M0.6 preview render, izleme ve teknik doğrulama.
7. Üretim kalitesi final render.
8. Plan `narrated_theory` ise M0.3 script ve M0.4 TTS zinciri.
9. Sonuç ve hata kayıtlarının arşivlenmesi.

### Zorunlu kabul listesi

- [x] Gerçek kaynak video Drive'a tamamen yüklenmiş ve ffprobe/ffmpeg tarafından okunmuş.
- [x] Analiz, gerçek video için `hot_moments.json` üretmiş.
- [x] En az bir hot moment görsel olarak incelenmiş.
- [x] Seçilen moment ve onaylayan kişi kaydedilmiş.
- [x] Ana mod `original_audio_subtitles` olarak kaydedilmiş.
- [x] Seçilen klibin orijinal sesinden zaman kodlu SRT üretilmiş.
- [x] Render orijinal ses stream'ini korumuş ve TTS kullanmamış.
- [ ] İsteğe bağlı `narrated_recap`: Gemini script üretimi ve insan onayı doğrulanmış.
- [ ] İsteğe bağlı `narrated_recap`: Gemini TTS ve ses onayı doğrulanmış.
- [x] Preview render 540×960 olarak üretilmiş ve izlenmiş.
- [ ] Final render 1080×1920 olarak üretilmiş ve izlenmiş.
- [ ] Altyazılar okunabilir ve konuşmayla kabul edilebilir ölçüde senkron.
- [ ] İsteğe bağlı `narrated_recap`: voice-over anlaşılır ve ses dengesi kabul edilmiş.
- [ ] Teknik doğrulama video ve ses stream'lerini başarılı bulmuş.
- [ ] Render manifestindeki tüm üst-akış onay koşulları `True`.
- [ ] `final_short.mp4` hiçbir platforma otomatik yüklenmemiş.
- [ ] Çalıştırma özeti ve hata dosyaları saklanmış.

### PoC kanıt paketi

Başarılı koşudan sonra aşağıdakiler korunmalı:

```text
results/hot_moments.json
results/moment_selection.json
results/render/subtitles.srt
results/render/subtitles_dynamic.ass
results/render/final_short.mp4
results/render/render_manifest.json
# Yalnızca isteğe bağlı narrated_recap modunda:
results/selected_script.json
results/selected_voiceover.json
results/tts/gemini_tts.wav
```

### M0.5 açık riskleri

| Risk | Etki | Mevcut önlem | Kapanış yöntemi |
|---|---|---|---|
| Gemini model ID veya ücretsiz kota gerçek hesapta farklı | Script/TTS durabilir | Capability discovery, rota fallback ve kota defteri | Colab'da gerçek çağrı ve manifest incelemesi |
| Gemini TTS preview kalitesi yetersiz | Ses doğal gelmeyebilir | Sağlayıcı bağımsız TTS sözleşmesi | ElevenLabs/Piper ile kontrollü kalite karşılaştırması |
| Uzun video yalnızca ilk 240 saniyeden analiz edilir | Güçlü an kaçabilir | `CLIP_START_SEC` ve `CLIP_DURATION_SEC` ayarlanabilir | Kaynak süresine uygun analiz kapsamı seçmek |
| Hot moment önizlemesi zayıf | Yanlış an onaylanabilir | Çoklu aday ve skor açıklaması | Preview klip üretimiyle M0.2'yi kapatmak |
| Altyazı hizalama fallback'e düşer | Okuma deneyimi bozulabilir | Word timestamp ve proportional fallback | Gerçek final videoda manuel senkron kontrolü |
| Colab runtime/Drive bağlantısı kesilir | Ara ürün kaybı | Drive'a kalıcı artefakt yazımı | Faz bazlı devam etme testi |

## 4b. M0.05 — Faz 0 kaynak işleme stratejisi (ZORUNLU kapı)

**Durum:** `verification_required`
**Kanonik modül:** [local_pipeline/source_strategy.py](local_pipeline/source_strategy.py)
**Artefaktlar:** `results/source_assessment.json`, `results/processing_plan.json`

Kaynak video seçildikten sonra iki üretim modundan biri **insan tarafından** seçilmeden
hiçbir alt faz çalışmaz. Notebook'ta Faz 0 → 0a → 0b → 0c hücreleri bu kapıyı kurar.

| Mod | Davranış | Üretim API'si |
|---|---|---|
| `direct_clip_series` | Çoklu hot moment → çoklu Short, orijinal ses korunur, mevcut dinamik aktif-kelime altyazı + render hattı | **Çağrılmaz** (script/TTS fazları yapısal olarak kapalı) |
| `narrated_theory` | Seçilen momentler görsel kanıt/B-roll; kullanıcının teori/yorum açısından mevcut Gemini rotasıyla script; script → TTS → render onay zinciri değişmeden işler | Gemini script + TTS |

### Mevcut yetenekler

- [x] Kaynak ölçümü (süre, çözünürlük, ses akışı, boyut) ve `probe_media` fallback'i.
- [x] Kaynak parmak izi (`ad|boyut|baş 1MiB|son 1MiB` SHA-256) — büyük Drive dosyalarında hızlı.
- [x] İki mod için deterministik yerel uygunluk skoru, gerekçe listesi ve engel listesi.
- [x] Öneri **yalnızca tavsiye**: `selected_mode=None`, `auto_selected=False`, otomatik seçim/onay yok.
- [x] Panelde kaynak bilgisi, iki mod skoru, gerekçeler, çıktı sayısı, hedef süre, moda özel seçenekler.
- [x] Creator Console'da plan oluşturulduktan sonra mod, çıktı sayısı, hedef süre, moment
      sıraları, teori açısı, kadraj, altyazı teması, önizleme modu ve plan notları kalıcı
      olarak düzenlenebilir; her kayıt eski onayı düşürür ve yeniden inceleme gerektirir.
- [x] Console faz sekmelerinde kaynak değerlendirmesi, video analizi, script, TTS ve render
      manuel başlatılabilir/yeniden çalıştırılabilir; Moment Explorer analiz düğmesi artefakt
      mevcut olsa da görünür kalır.
- [x] Proje üstü otomatik akış sıradaki güvenli işi çalıştırır ve plan seçimi, moment seçimi,
      script/ses/render onayı gibi insan kapılarında açıkça durarak aynı düğmeyle devam eder.
- [x] `processing_plan.json` doğuştan `review_required`; onay yalnızca hash-bağlı
      `approval.approve_artifact(plan, 'processing_plan', ...)` ile verilir.
- [x] Karar bloğu (`decision`) onay hash'inin girdisidir: kaynak, mod, momentler, teori açısı,
      çıktı sayısı, hedef süre ve render ayarı değişirse onay **otomatik** düşer.
- [x] Onaylı plan alt fazların kapısıdır (`plan_allows_phase`); plan yoksa/onaysızsa hepsi durur.
- [x] Render öncesi canlı yapılandırma kayması (`check_config_drift`) kontrolü.
- [x] Çoklu çıktı izolasyonu: her Short kendi `render/output_NN/` dizinine yazar; tek çıktıda
      tarihsel `render/` yolu korunur.
- [x] `production_ready` üretilemez; elle açılmış faz kapısı doğrulamada reddedilir.

### Kapanış kriterleri

- [ ] Gerçek Colab koşusunda Faz 0 paneli okundu ve mod **bilinçli** seçildi.
- [ ] `processing_plan.json` gerçek hash ile onaylandı ve alt fazların açıldığı gözlendi.
- [ ] `direct_clip_series` gerçek koşuda sıfır Gemini/TTS çağrısı yaptığı doğrulandı.
- [ ] Kaynak videoyu değiştirip onayın gerçekten düştüğü canlı ortamda gözlendi.
- [ ] Çoklu çıktı planında ek yuvaların ayrı dizinlerde raporlandığı görüldü.

### Bilinen sınır

Tek koşuda yalnızca **ilk** çıktı yuvası render edilir. 2..N yuvaları izole dizinleriyle
raporlanır ama render edilmez, çünkü her yuva kendi insan-onaylı moment seçimini gerektirir
ve onaysız bir momenti render etmek onay kapısını delerdi. Yuva başına moment seçimi
üreticisi bir sonraki milestone işidir.

## 5. M0.0 — Planlama ve güvenlik ilkeleri

**Durum:** `in_progress`

### Tamamlananlar

- [x] Kullanıcı tarafından sağlanan medya ilkesi.
- [x] Otomatik yayın yasağı.
- [x] İnsan-onaylı script, ses ve render kapıları.
- [x] `execution_status` ile `artifact_status` ayrımı.
- [x] Secret'ların notebook çıktısı ve artefaktlara yazılmaması.
- [x] Sağlayıcı bağımsız ve maliyet duyarlı routing yaklaşımı.
- [x] Araştırma yan kolunun ana pipeline'dan ayrılması.

### Açık işler

- [ ] MVP PRD ve kabul kriterlerinin kullanıcı tarafından son onayı.
- [ ] Hedef Short türlerinin önceliklendirilmesi: recap, theory, scene explanation vb.
- [ ] Telif ve dönüştürücü kullanım kontrol listesinin ürün akışına eklenmesi.
- [ ] Private upload yetkilendirme sınırının ileride ayrıca tanımlanması.

## 6. M0.1 — Tek-video analiz prototipi

**Durum:** `verification_required`

### Mevcut yetenekler

- [x] MP4 kaynak okuma ve klip çıkarma.
- [x] FFmpeg/ffprobe fallback çözümleme.
- [x] Faster-Whisper transcript.
- [x] PySceneDetect sahne tespiti.
- [x] Librosa ses enerjisi.
- [x] Çoklu-sinyal hot moment skoru.
- [x] Sabit şemalı `hot_moments.json`.
- [x] Yerel gerçek medya smoke testi.

### Kapanış kriterleri

- [x] Colab'da gerçek tam kaynak yoluyla çalışma.
- [ ] Analiz kapsamının videonun amaçlanan bölümünü içerdiğinin doğrulanması.
- [ ] Aday moment başlangıç/bitiş zamanlarının görüntüyle doğrulanması.
- [ ] En az bir kabul ve bir ret kararının kaydedilmesi.

### Kanıt

- `local_pipeline/video_analysis.py`
- `experiments/colab/01_video_analysis_hotmoment.ipynb`
- `verify_local.py` içindeki `LOCAL_SMOKE_TEST_OK`

## 7. M0.2 — Moment Explorer v0

**Durum:** `verification_required`

### Mevcut yetenekler

- [x] Birden fazla sıralı aday moment.
- [x] Moment skorları, zaman aralıkları ve nedenler.
- [x] Rank ile moment seçimi.
- [x] Onaysız momentte ücretli script çağrısını engelleme.
- [x] Her aday için notebook içinde oynatılabilir MP4 preview.
- [x] Seçim ve reddedilen rankların `moment_selection.json` içine kalıcı kaydı.
- [x] Seçimi analiz manifesti ve preview dosyası SHA-256 değerine bağlama.
- [x] Analiz veya preview değişirse seçimi geçersiz kılma.

### Açık işler

- [ ] Gerçek Colab videosunda tüm preview'ları baştan sona izleme.
- [ ] Favori kararının ayrı kaydı.
- [ ] Birden fazla moment seçme ve sıralama.
- [ ] Önceki/sonraki bağlamı genişletme.
- [ ] Karakter, duygu, aksiyon, diyalog ve spoiler filtreleri.

## 8. M0.3 — Prompt ve Script Studio v0

**Durum:** `verification_required`

### Mevcut yetenekler

- [x] Gemini varsayılan script sağlayıcısı.
- [x] Seçili moment ve transcript tabanlı prompt.
- [x] İçerik modu, ton ve hedef süre ayarları.
- [x] Tek sağlayıcı çağrısı ve sıralı fallback politikası.
- [x] Script inceleme ve hash-tabanlı onay.
- [x] Script değişince onayın geçersiz olması.

### Açık işler

- [ ] Gerçek `GeminiKey` ile başarılı script üretimi.
- [ ] İlk 2 saniyelik hook kalite kontrolü.
- [ ] Hedef süreye göre gerçek konuşma uzunluğu kontrolü.
- [ ] Başlangıç planındaki üç varyant: viral, analitik, sinematik.
- [ ] Cümle bazında kaynak moment/evidence bağlantısı.
- [ ] Theory Guard v0.

## 9. M0.4 — Voice Studio v0

**Durum:** `verification_required`

### Varsayılan rota

```python
RUN_GOOGLE_TTS = True
TTS_PROVIDER = "gemini"
GEMINI_TTS_LANGUAGE_CODE = "tr-TR"
```

### Mevcut yetenekler

- [x] Gemini TTS model rotası ve fallback.
- [x] PCM çıktısını geçerli WAV'a sarma.
- [x] Codec, örnekleme hızı, kanal, süre ve boş-dosya doğrulaması.
- [x] Script hash'ine bağlı voice-over artefaktı.
- [x] Notebook ses oynatıcısı.
- [x] Ayrı dinleme ve SHA-256 onay adımı.
- [x] Yeni ses/model üretiminde eski onayı geçersiz kılma.
- [x] gTTS çıktısının final onayını engelleme.
- [x] ElevenLabs, OpenAI ve Piper için alternatif sağlayıcı sözleşmesi.

### Kapanış kriterleri

- [ ] Gerçek Gemini TTS çağrısı başarılı.
- [ ] WAV baştan sona dinlendi.
- [ ] Türkçe telaffuz kabul edilebilir.
- [ ] Hız, ton, duraklama ve ifade hedef içeriğe uygun.
- [ ] Ses süresi hedef Short süresiyle uyumlu.
- [ ] En az bir başarısızlık/fallback davranışı gerçek ortamda gözlemlendi.

## 10. M0.6 — Temel edit ve render

**Durum:** `verification_required`

### Mevcut yetenekler

- [x] 9:16 blurred-background ve center-crop modları.
- [x] Voice-over/orijinal ses karışımı ve audio ducking.
- [x] Altyazı üretimi ve videoya gömme.
- [x] Kelime zaman damgalı dinamik ASS ve konuşulan kelimeyi sarı takip etme.
- [x] Kısa kelime grupları, güvenli font/boyut/kenar çizgisi ve SRT fallback'i.
- [x] ASS olayları arasında sıfır zaman çakışması; yeni kart başlarken eski kart zorunlu kapanır.
- [x] İçerik-temalı font profili: GOT/HOTD/Targaryen/Westeros için doğrulanmış `Marcellus SC`; `Cinzel` karşılaştırma/geri dönüş seçeneği.
- [x] Moment başlangıcını yalnızca önceki sahneye, bitişini yalnızca sonraki sahneye genişleten konuşma-bağlamlı kesim politikası.
- [x] Preview ve final kalite modları.
- [x] FFprobe yokken FFmpeg metadata fallback'i.
- [x] Çözünürlük, duration, video/audio stream doğrulaması.
- [x] Script, voice-over, hash ve teknik kontrolü zorunlu render onayı.
- [x] Yerel gerçek FFmpeg smoke testi.

### Kapanış kriterleri

- [x] Gerçek içerikte preview izlendi.
- [x] Crop önemli yüz/aksiyonu kesmiyor.
- [x] Orijinal ses sürekliliği kabul edildi.
- [ ] Altyazı taşma, font ve zamanlama kontrolü geçti.
- [ ] 1080×1920 final render teknik ve görsel kontrolden geçti.

## 11. R0.1 — YouTube araştırma yan kolu

**Durum:** `verification_required`

### Mevcut yetenekler

- [x] `YoutubeKEY` Secrets entegrasyonu.
- [x] Anahtar kelimeyle `search.list` keşfi.
- [x] Arama sayısı üst sınırı ve önbellek.
- [x] Video tekrarlarını birleştirme.
- [x] Görüntülenme hızı, güncellik, etkileşim ve tekrar sinyalli trend skoru.
- [x] Aday kanal sıralaması ve kanal geçmişi toplama.
- [x] Sabit şemalı CSV/JSON çıktıları.
- [x] Ana pipeline'a veri beslememe sözleşmesi.

### Kapanış kriterleri

- [ ] Colab'da gerçek anahtar kelime keşfi çalıştırıldı.
- [ ] `discovered_videos.csv` ve `discovered_channels.csv` insan tarafından incelendi.
- [ ] Kota raporu Google Cloud Console ile karşılaştırıldı.
- [ ] Arama kalitesi için kabul/ret örnekleri kaydedildi.

## 12. R0.2 — Viral potansiyel araştırması

**Durum:** `in_progress`

### Mevcut yetenekler

- [x] Boş/bozuk CSV'lerde güvenli `insufficient_data` davranışı.
- [x] Sabit veri sözleşmesi.
- [x] Baseline ve holdout değerlendirme kodu.
- [x] Eksik veya yanlış LLM çıktısını reddetme.

### Açık işler

- [ ] Yeterli gerçek kanal/video geçmişi.
- [ ] Baseline sonuçlarının gerçek veride değerlendirilmesi.
- [ ] Kanal ortalamasına göre kalibrasyon.
- [ ] Tahmin aralıkları ve güven seviyesi doğrulaması.

## 13. Daha sonraki release fazları

### M0.7 — Private upload ve yayın güvenliği

**Durum:** `not_started`

- Private/unlisted upload ayrı ve açık kullanıcı yetkisi gerektirir.
- Public yayın ikinci bir açık onay olmadan yapılamaz.
- Bu milestone mevcut notebook tarafından uygulanmaz.

### M0.8 — Analytics ve öğrenme

**Durum:** `not_started`

- İlk 3/30 saniye retention
- Average percentage viewed ve completion
- Rewatch, share, comment, like ve subscriber conversion
- Hook, kullanılan moment, script değişikliği ve yayın zamanı

### M0.9 — Creator Console pre-alpha

**Durum:** `planned`  
**Amaç:** Notebook hücrelerini son tüketiciye göstermeden kaynak video alımı,
işleme stratejisi seçimi, moment inceleme, script/ses onayı ve render takibini tek
bir web arayüzünden yönetmek. İlk sürüm yerel/özel tek kullanıcı pre-alpha'dır;
mimari sınırlar gelecekte çok kullanıcılı kullanıma geçişi engellememelidir.

#### Görsel yön ve tasarım sistemi

- Siyah ve kömür tonlarının baskın olduğu koyu arayüz.
- Turuncu ana aksiyon, aktif seçim, ilerleme ve vurgu rengi.
- Beyaz/açık gri okunabilir metin; hata, uyarı ve başarı renkleri turuncudan ayrılır.
- Sinematik fakat sade görünüm; yoğun kontrol ekranlarında dekorasyon işlevin önüne geçmez.
- Tutarlı renk, boşluk, tipografi, radius, gölge ve durum token'ları.
- Klavye kullanımı, görünür odak, yeterli kontrast ve renk dışında ikinci durum işareti.
- Önce masaüstü üretim deneyimi; mobilde en azından izleme ve onay akışları responsive çalışır.

#### Pre-alpha ekranları

1. Proje/kaynak video oluşturma ve yükleme.
2. Kalıcı Kayıtlar: önceki projeleri açma, ad/not düzenleme ve kaldığı yerden devam etme.
3. Phase 0 kaynak değerlendirme ve işleme stratejisi seçimi.
4. İş akışı özeti: bekliyor, çalışıyor, inceleme gerekli, başarısız, tamamlandı.
5. Direct Clip Series için moment kartları, preview ve çoklu seçim.
6. Narrated Theory için teori açısı, kanıt zaman kodları ve script inceleme.
7. Voice Studio: sağlayıcı, ses preview'u ve hash-bağlı onay.
8. Render Studio: altyazı/crop ayarı, preview, teknik sonuç ve final onayı.
9. Üretilen Short'lar, manifestler, hata ayrıntıları ve indirme geçmişi.
10. Ayarlar: proje, dil, caption teması, model rotası ve kota durumu; secret değeri gösterilmez.

#### Uygulama ve pipeline sınırı

- Arayüz mevcut kanonik `local_pipeline` fonksiyonlarını kullanır; notebook mantığı frontend'e kopyalanmaz.
- UI ile uzun süren analiz/render işleri arasında job/orchestration katmanı bulunur.
- İşler tekrar çalıştırılabilir ve idempotent olmalı; sayfa yenilenmesi çalışan işi kaybettirmemeli.
- Her proje aşama bazında geri sarılabilir, script insan tarafından düzenlenebilir,
  kaynak korunarak veya tamamen sıfırlanabilir ve güvenli biçimde silinebilir. Geri
  sarılan/sıfırlanan çıktılar kalıcı kaybolmak yerine proje revizyon arşivine taşınır.
- Her ekran mevcut JSON artefakt sözleşmelerini ve `execution_status` / `artifact_status` ayrımını korur.
- Script, ses, processing plan ve render onayları mevcut SHA-256 bağlı kapılardan geçer.
- API anahtarları yalnızca sunucu/Secrets tarafında tutulur; tarayıcıya, loga veya artefakta yazılmaz.
- Otomatik upload/yayın eklenmez; mevcut `not_uploaded` politikası korunur.

#### Gelecekte çok kullanıcılı kullanıma hazırlık

Pre-alpha tek kullanıcıyla çalışsa da aşağıdaki alanlar ve sınırlar baştan tasarlanır:

- `user_id`, `workspace_id`, `project_id` ve `job_id` ile sahiplik modeli.
- Workspace/proje bazlı dosya ve artefakt namespace'i; kullanıcılar arası yol sızıntısı olmaması.
- Kimlik doğrulama ve RBAC eklenebilecek servis sınırı; pre-alpha public internete kimlik doğrulamasız açılmaz.
- Kullanıcı/proje bazlı provider secret referansları; secret değerlerinin veritabanına düz metin yazılmaması.
- Kuyruk, eşzamanlılık limiti, iptal, yeniden deneme ve kota muhasebesi için genişletilebilir job modeli.
- Onay, ayar değişikliği ve provider çağrıları için değiştirilemez audit-event sözleşmesi.
- Yerel/Drive depolamadan nesne depolamaya geçirilebilen storage adapter'ı.
- Tek süreçli pre-alpha'dan worker tabanlı çalışmaya geçişi engellemeyen API sınırı.

#### Kapanış kriterleri

- [x] Siyah–turuncu token tabanlı tasarım sistemi uygulanmış.
- [x] Phase 0'dan preview rendera kadar tek kullanıcı web akışı tamamlanmış (fixture ile).
- [x] Direct Clip Series ve Narrated Theory seçimleri doğru pipeline rotasına bağlanmış.
- [x] Uzun işler sayfa yenilense/backend yeniden başlasa bile durumunu korumuş.
- [x] İnsan onay kapıları UI üzerinden atlanamamış.
- [x] API anahtarları istemci paketinde, ağ cevabında, logda veya artefaktta görünmemiş.
- [x] Başarısız iş uygulanabilir hata ve güvenli yeniden deneme sunmuş.
- [ ] Masaüstü ana akış ve mobil izleme/onay görünümü görsel kontrolden geçmiş.
- [ ] En az bir gerçek kaynak video arayüzden preview Short'a dönüştürülmüş.
- [ ] Erişilebilirlik, responsive davranış ve temel performans smoke testleri geçmiş.
- [x] Çok kullanıcılı hazırlık için sahiplik ve storage izolasyonu otomatik test edilmiş.
- [x] Önceki projeler yeniden başlatma sonrasında listelenmiş, açılmış ve metadata düzenlemesi kalıcı olmuş.

#### Uygulama kanıtı (pre-alpha)

**Stack kararı:** sıfır yeni bağımlılık. Backend `http.server.ThreadingHTTPServer`,
frontend build-step'siz ES modülleri + CSS custom property'leri. Gerekçe: proje
zaten ağır bilimsel bir yığın taşıyor (ffmpeg/whisper/scenedetect/librosa); buna
bir web framework'ü + Node toolchain eklemenin bakım maliyeti, tek dosyalık bir
router ve build'siz bir frontend'den çok daha yüksek. Threading gerçek olduğu için
eşzamanlılık ve çift-gönderim davranışı dürüstçe test edilebiliyor.

**Dosyalar:** `creator_console/` (`ids`, `storage`, `secrets_ref`, `audit`, `jobs`,
`pipeline_service`, `app`, `api`, `server`, `fixtures`, `static/`), `verify_console.py`,
`CONSOLE.md`.

**Komut:** `python -m creator_console` (tek dokümante komut, `--demo-fixtures` ile
fixture'lı akış).

**Test kanıtı:** `python verify_console.py` → `CREATOR_CONSOLE_VERIFICATION_OK`,
çıkış kodu 0. 15 bölüm: kimlik/dosya-adı sertleştirme, storage namespace izolasyonu
(6 traversal saldırısı reddedildi), fixture akışı (kaynak→strateji→iş→inceleme→çıktı),
direct_clip_series'te **0 sağlayıcı çağrısı**, narrated_theory onay zinciri,
bayat onay tespiti, idempotency + 6 eşzamanlı worker'dan tek claim, çökme/restart
kurtarma, çapraz-proje erişim reddi, secret sızıntısı (API/artefakt/bundle/resolver),
7 bozulma türü, kısmi render, gerçek soket üzerinden HTTP, frontend sözleşmesi,
artefakt sözleşmesi.

**Bulunan ve düzeltilen kusur:** `recover_abandoned()` yalnızca süresi dolmuş
lease'leri kurtarıyordu; backend restart'ında, lease'i hâlâ taze görünen ama onu
tutan süreç ölmüş işler kalıcı olarak `running` kalıyordu. Başlangıç yolu artık
`ignore_lease=True` ile çalışıyor (çalışma anındaki periyodik kurtarma hâlâ canlı
lease'lere saygı duyuyor).

**Gerçekten çalıştırılan:** ffmpeg (fixture video + render), faster-whisper,
PySceneDetect, librosa, gerçek soket üzerinden HTTP, gerçek thread'ler, atomik
dosya yazımı, gerçek SHA-256 onay kapıları.
**Mock'lanan:** tüm sağlayıcılar (Gemini/OpenAI/Anthropic/ElevenLabs) enjekte
edilmiş test ikizleridir; hiçbir ağ çağrısı yapılmaz, hiçbir kimlik bilgisi gerçek
değildir, hiçbir şey yüklenmez/yayınlanmaz.

**Kalan açıklar:** gerçek kaynak videoyla arayüzden preview Short üretimi ve
mobil görsel kontrol hâlâ elle yapılmalı; erişilebilirlik denetimi otomatik değil.

#### Bu milestone'da kapsam dışı

- Gerçek çok kiracılı production deployment
- Abonelik/faturalandırma
- Agency/white-label
- Native mobil uygulama
- Public API/provider marketplace
- Otomatik public YouTube yayını

Bu maddeler M1.0, M1.1 ve M2.0 aşamalarında ele alınacaktır. Pre-alpha arayüzü
gelecekteki sistemi taklit ederek sahte başarı göstermemeli; henüz uygulanmayan
çok kullanıcılı yetenekleri açıkça “planlandı” olarak göstermelidir.

### M1.0 — Çoklu kanal ve Channel Brain

**Durum:** `not_started`

- Çoklu kanal/concept
- Kanal bazlı ayarlar
- Channel Brain v0
- Scene Reuse Guard

### M1.1 — Web ve mobil beta

**Durum:** `not_started`

- Responsive web
- Mobil inceleme/onay
- Bildirimler
- Workspace rolleri ve audit log

### M2.0 — SaaS ve ölçek

**Durum:** `deferred`

- Multi-workspace/RBAC
- Kullanım ve abonelik takibi
- Agency/white-label
- Public API ve provider marketplace
- Lokalizasyon ve çoklu dil varyantları

## 13b. Adversarial fault-injection ve kurtarma turu

**Durum:** `verification_required` (mock sağlayıcılarla geçti; canlı ortamda tekrarlanmalı)
**Suit:** [verify_faults.py](verify_faults.py) → `FAULT_INJECTION_SUITE_OK`, 46 senaryo, çıkış kodu 0

### Bulunan ve düzeltilen kusurlar

| # | Kusur | Etki | Düzeltme |
|---|---|---|---|
| 1 | `sanitize_google_error()` kimlik bilgisi kapsamda değilken yalnızca **desen** eşleştiriyordu. `JobStore.finish()`, `AuditLog.record()` ve `ApiError` tam olarak bu yolu kullanıyordu. | `AIza…` desenine uymayan bir sağlayıcı token'ı (Groq/Azure/Cloudflare) bir istisna mesajıyla **iş kaydına, audit olayına ve HTTP yanıtına düz metin yazılabiliyordu**. Yeniden üretildi. | Yeni `creator_console/redaction.py`: süreç-yerel kayıt defteri. `SecretResolver` çözdüğü her değeri kaydediyor; `jobs`/`audit`/`api` artık `scrub()` üzerinden geçiyor. Defter yalnızca bellekte, `registered_count()` **sayı** döner, değer değil. |
| 2 | Onay geçerliliği artefakt başına bağımsız hesaplanıyordu. | Script onaylandıktan sonra **strateji planı değiştirilince** script'in kendi hash'i değişmediği için onay hâlâ "geçerli" görünüyordu. Aynısı ses onayı için de geçerliydi. | `PipelineService.approval_chain()`: `processing_plan → [moment_selection] → script → voiceover → render` zinciri boyunca `effective_valid` hesaplanıyor. Mod-farkındalı (clip-series'te script/ses zincirde yok). Hiçbir şey silinmiyor — audit geçmişi ve `human_approval` blokları aynen duruyor. |
| 3 | `plan_allows_phase()` yalnızca `status == "approved"` kontrol ediyordu. | Onaylandıktan sonra karar bloğu değiştirilen (veya elle düzenlenen) bir plan **tüm faz kapılarını açık tutuyordu**. | `check_approval()` ile onayın hâlâ mevcut kararı **bağladığı** doğrulanıyor; bağlamıyorsa tüm fazlar duruyor. |
| 4 | Windows'ta `os.replace` eşzamanlı okuyucu varken `PermissionError` veriyordu; yeniden deneme yoktu. | Yoğun okuma altında artefakt yazımı **sessizce başarısız olabiliyordu**. | Jitter'lı üstel geri çekilmeyle 25 deneme; bütçe biterse hata **yükseltiliyor** (asla yutulmuyor). Temp dosyası artık thread id de taşıyor. |

### Fault-injection matrisi (7 aile / 46 senaryo)

| Aile | Senaryo sayısı | Sonuç |
|---|---|---|
| 1. Bozuk artefaktlar | 6 | Hepsi raporlanıyor; bozuk plan tüm kapıları kapatıyor; sayfa çökmüyor |
| 2. Kısmi render | 6 | 0 bayt/kesik MP4, ffmpeg hatası, doğrulama başarısız → asla onaylanamaz |
| 3. Yinelenen çalıştırma | 5 | 8 eşzamanlı gönderim → 1 iş, **1 sağlayıcı çağrısı**; 10 worker → 1 claim |
| 4. Sahiplik izolasyonu | 6 | 8 traversal/mutlak yol/UNC saldırısı, 7 düşmanca kimlik, 6 güvensiz dosya adı |
| 5. Bayat onaylar | 8 | Strateji/script/ses/kaynak değişimi → bağımlı onaylar geçersiz, audit korunuyor |
| 6. Yenileme/restart/çökme | 8 | Taze lease'li restart, süresi dolmuş lease, sağlıklı iş korunuyor, atomik yazım |
| 7. Secret sızıntısı | 9 | İş kaydı, audit (iç içe), 7 API yanıtı, disk, bundle, stdout/stderr |

### Kanıtlanan kurtarma ve idempotency garantileri

- Aynı girdilerle ikinci gönderim **yeni iş üretmez** ve **ikinci sağlayıcı çağrısı yapmaz**.
- Bir işi aynı anda yalnızca **tek** worker claim edebilir (10 thread ile doğrulandı).
- Tamamlanmış iş yeniden kuyruğa **alınamaz**.
- Backend restart'ında hiçbir iş `running` takılı kalmaz; lease taze görünse bile kurtarılır.
- Çalışma anındaki periyodik kurtarma **sağlıklı** işi öldürmez.
- Atomik yazım: okuyucular hiçbir koşulda **yarım doküman görmez**; patolojik çekişmede yazım açıkça hata verir.
- Audit dosyasının son satırı yarım kalsa bile geri kalan kayıtlar okunur.

### Kalan riskler (test edilmedi)

- Gerçek çok-süreçli worker'lar (pre-alpha tek süreç varsayıyor; `recover_abandoned(ignore_lease=True)` bu varsayıma dayanıyor).
- Gerçek sağlayıcı hata gövdeleri ve gerçek anahtar formatları (hepsi mock).
- Disk dolması, dosya sistemi izin hataları, ağ depolama gecikmesi.
- Headless tarayıcı yok; frontend JS render fonksiyonları hiçbir test tarafından çalıştırılmıyor.
- Çok saatlik render sırasında lease uzatma (heartbeat) canlı ortamda denenmedi.

## 13c. Console sertleştirme turu — kalan riskler kapatıldı

**Durum:** `verification_required` (mock sağlayıcılarla geçti)
**Suitler:** `verify_browser.py` → `BROWSER_SUITE_OK` (42/42) · `verify_faults.py` → `FAULT_INJECTION_SUITE_OK` (70/70)

### Kapatılan riskler

| Önceki risk | Şimdi |
|---|---|
| Headless tarayıcı yok; JS render fonksiyonları hiç çalıştırılmıyor | Gerçek Chromium 10 ekranı, onay akışlarını, çift tıklamayı, yenilemeyi, medya oynatmayı ve mobil düzeni sürüyor |
| Tek süreç varsayımı (`recover_abandoned(ignore_lease=True)`) | `creator_console/worker.py` gerçek ayrı OS süreci; kurtarma **sahip-farkındalı** (canlı worker'ın işi elinden alınmaz) |
| Uzun işlerde lease uzatma denenmedi | `_Heartbeat` lease'i yeniler; canlı worker öldürülerek doğrulandı |
| Disk dolması / izin hataları / yavaş depolama test edilmedi | `creator_console/faults.py` ile ENOSPC, EACCES, yavaş depolama ve yarıda kesilen yazım enjekte ediliyor |
| Ham sağlayıcı istisnası yanıta sızabilir | `creator_console/errors.py` kapalı katalog; ham metin hiç çıkmaz, `debug_id` ile sunucu günlüğüne gider |

### Bulunan ve düzeltilen kusurlar

| # | Kusur | Nasıl bulundu | Düzeltme |
|---|---|---|---|
| 5 | Frontend `hashchange`/`popstate` dinlemiyordu: tarayıcı İleri/Geri ve açık sayfada `#ekran` bağlantısı **hiçbir şey yapmıyordu** | İlk Playwright koşusu — ve aynı kusur testimi de yanlış geçirmişti (10 ekranın hepsi "render oldu" görünüyordu, oysa hiç değişmemişti) | `hashchange`/`popstate` dinleyicileri + `goto()` artık `pushState` kullanıyor; test ekranı **kendi başlığıyla** doğruluyor |
| 6 | Mobil görünümde **645px yatay taşma** (phase0), 250px (jobs) — mobil inceleme deneyimi bozuk | Playwright `scrollWidth - clientWidth` ölçümü | Grid/flex çocuklarına `min-width: 0`, `.pre`/`.table-scroll`/`video`'ya `max-width: 100%` |
| 7 | Ham istisna metni HTTP yanıtına gidiyordu; `SecretResolver`'ı **atlayan** bir token (SDK'nın kendi hata gövdesi) redaksiyon defterinde olmadığı için sızabilirdi | Fault-injection senaryosu | Kapalı hata kataloğu (statik mesaj + kod + `debug_id`); ayrıca yüksek-entropi backstop (onay hash'leri hariç tutuluyor) |
| 8 | `recover_abandoned(ignore_lease=True)` gerçek worker'larla **yanlıştı**: sunucu yeniden başlarken canlı bir dış worker'ın işini öldürürdü | Çok süreçli harness | Sahip-farkındalı kurtarma: `owner_pid`/`owner_host` kaydediliyor, süreç canlılığı Win32/POSIX ile sorgulanıyor |
| 9 | `patch_atomic_write` yalnızca `storage` modülünü yamalıyordu; `jobs`/`pipeline_service` adı import anında bağladığı için **arıza hiç tetiklenmiyordu** (asla başarısız olamayan test) | Depolama arızası senaryoları sessizce geçti | Adı tutan tüm modüller yamalanıyor |

### Fault-injection matrisi eklenen aileler

| Aile | Senaryo | Sonuç |
|---|---|---|
| 8. Ayrı süreçli worker'lar | 7 | Gerçek OS süreci işi çalıştırır; kontrollü noktada öldürülen worker'ın işi kurtarılır; canlı worker'ın işi **elinden alınmaz**; 3 yarışan worker'dan biri çalıştırır; worker başlangıç kurtarması yapmaz |
| 9. Depolama arızaları | 8 | ENOSPC → `failed`+recoverable; EACCES → HTTP 507 + retryable, iç ayrıntı sızmaz; yarıda kesilen yazım → hata yükselir **ve** sonraki okuma bozuk raporlar; yavaş depolamada idempotency bozulmaz |
| 10. Yapısal hata kodları | 7 | 5 hata yolu doğru katalog kodu döner; gövdede traceback/dosya yolu yok; katalog mesajları statik; `SecretResolver`'ı atlayan token yanıta girmez, günlükte redakte edilir, onay hash'i redakte **edilmez** |

### Tarayıcı testi kapsamı (42 kontrol)

10 ekran kendi başlığıyla · JS hatası yok · hesaplanmış turuncu/koyu token'lar · odak erişilebilirliği ·
hash navigasyonu ve tarayıcı Geri · Faz 0 iki mod kartı · mod seçimi tek başına plan **oluşturmaz** ·
yanlış hash reddedilir + kullanıcıya bildirilir · UI'daki hash gerçek hash'tir · doğru hash onaylar ·
çift tıklama tek iş üretir · iş durumu yenilemeden sonra korunur ve yeniden gönderilmez ·
moment kartları + gerçek video yüklenmesi (`readyState=4`) · kapalı faz gerekçesiyle görünür ·
yapısal hata kodu + `debug_id`, traceback yok · bayat onay UI'da GEÇERSİZ · secret DOM/bundle/window'da yok ·
mobilde yatay taşma yok (4 ekran) · masaüstünde iki sütun

### Kalan riskler

- Gerçek sağlayıcı hata gövdeleri ve gerçek anahtar formatları hâlâ mock.
- Çok makineli worker'lar (farklı host) test edilmedi; `process_alive` başka hostu **canlı** sayar (güvenli varsayılan).
- Firefox/WebKit ve gerçek mobil cihaz test edilmedi (yalnızca Chromium + emülasyon).
- Erişilebilirlik denetimi otomatik değil (kontrast/odak sırası elle).
- Gerçek kaynak videoyla arayüzden preview Short üretimi hâlâ elle doğrulanmalı.

## 13d. Console yürütme deneyimi — otomatik akış ve manuel kontroller

**Durum:** `verification_required` (mock sağlayıcılarla geçti; gerçek kaynakla render üretildi, **insan görsel incelemesi bekliyor**)

### Otomatik akış durum makinesi (sunucuda kalıcı)

Artefakt: `results/automation.json` — [creator_console/automation.py](creator_console/automation.py)

```text
idle ──start──▶ running ──(makine fazı gönderildi)──▶ running
                  ├──(insan kapısı)──────────────────▶ blocked
                  ├──pause──────────▶ paused ──resume──▶ running
                  ├──cancel─────────────────────────▶ cancelled
                  ├──(ilerleme yok / dedup / hata)──▶ blocked
                  └──(render onayı geçerli)─────────▶ completed
```

**Beş insan kapısı** (otomasyon burada *daima* durur, asla geçmez):
`plan_selection` · `moment_selection` · `script_approval` · `voiceover_approval` · `render_approval`

**Üç bağımsız döngü freni:**
1. faz başına deneme üst sınırı (`MAX_PHASE_ATTEMPTS=1`) — bir makine fazı otomatik olarak en fazla bir kez gönderilir;
2. ilerleme-yok sayacı (`MAX_NO_PROGRESS_TICKS=3`) — pipeline imzası değişmeden geçen tick'ler sayılır;
3. terminal sonuçlar — dedup edilmiş gönderim, başarısız iş, artefakt üretmeyen koşu ve `insufficient_data` anında `blocked` yapar.

Otomasyon **hiçbir zaman** mod seçmez, moment seçmez, onay vermez.

### Eklenen manuel kontroller

| Sekme | Kontrol |
|---|---|
| Faz 0 | Kaynak değerlendirmesini çalıştır / yeniden çalıştır |
| Moment Explorer | **Düzenlenebilir:** klip başlangıcı, klip süresi, Whisper modeli, sahne dedektörü, top-N + çalıştır/yeniden çalıştır |
| Script Studio | Onaylı planın teori açısını gösterir; planın sağlayıcı rotasıyla script üretimini başlatır |
| Voice Studio | **Düzenlenebilir:** ses (10 Gemini sesi), dil kodu, anlatım yönergesi |
| Render Studio | Onaylanan plan ayarlarını gösterir (bilerek düzenlenemez — karar hash'inin parçası), başlat/yeniden çalıştır |

Her sekmede **`phaseStatusPanel`**: aktif iş, son sonuç, `artifact_status`, deneme sayacı, başarısızlık nedeni ve güvenli "Yeniden dene" düğmesi.

Bilinçli her tıklama yeni bir `run_request_id` üretir → **gerçek yeniden çalıştırma**; aynı id ile ikinci istek (çift tıklama / HTTP tekrarı) → **idempotent**.

### Moment Explorer sertleştirmesi

- Görünüm sınırında koşulsuz `scrub_host_paths`: namespace içindeki mutlak yol namespace-göreli hale gelir, dışındaki `<host-path-hidden>` olur. Yeni bir alan eklendiğinde bile host yolu sızamaz.
- Eski Windows mutlak preview yolları ya göreli medya rotasına çevrilir ya da `preview_problem` ile açıkça işaretlenir.
- Yeniden analiz sonrası bayat seçim UI'da kırmızı uyarıyla gösterilir; sessizce geçerli kalmaz.
- Kartlar ve videolar yenileme/restart sonrası korunur (tarayıcı testiyle doğrulandı).

### Gerçek kaynak uçtan uca önizleme (izole)

`python verify_real_preview.py` → `REAL_PREVIEW_OK`, çıkış kodu 0.

| Ölçüm | Değer |
|---|---|
| Kaynak | HOTD 1329s, 1920×1080, ses var (salt-okunur referans) |
| İzole depolama | `.console_data_realtest/workspaces/wsp_realtest/projects/prj_.../` |
| Analiz | 48.8s, 2 aday (rank 1 skor 92.6, 63.1s→105.8s) |
| Render | 12.2s, 2305 KB |
| ffprobe | h264 **540×960** (9:16 dikey) + aac 44100Hz, **42.77s** |
| Altyazı | `faster-whisper-word-alignment`, 9 cue, dinamik kelime vurgusu, Cinzel/GOT teması |
| Durum | **`review_required`** — final onay VERİLMEDİ |
| Sağlayıcı çağrısı | **0** |

**Çıktı yolu:**
`.console_data_realtest/workspaces/wsp_realtest/projects/prj_6ea42393b40be1d9/results/render/final_short.mp4`

Kullanıcının kendi projesi **değiştirilmedi**; orijinal kaynak dosya bozulmadı.

### Kalan riskler

- Üretilen önizleme **insan tarafından izlenmedi**: altyazı senkronu, crop kadrajı ve orijinal ses sürekliliği gözle doğrulanmalı.
- `narrated_theory` uçtan uca yalnızca mock Gemini ile koşuldu; gerçek script/TTS kalitesi doğrulanmadı.
- Otomatik akış tek süreçte test edildi; harici worker ile otomasyon birlikte çalışması test edilmedi.
- Yalnızca Chromium; Firefox/WebKit ve gerçek mobil cihaz yok.

## 13e. Console kullanılabilirlik turu — preflight ve ilerleme göstergesi

**Durum:** `verification_required`
**Amaç:** Colab Short üretim akışını **kod düzenlemeden** yerelde çalıştırmak.

### Eklenen: yerel ortam kontrolü (preflight)

[creator_console/status.py](creator_console/status.py) · `GET /api/preflight` · `GET /api/projects/<id>/preflight`

24 kontrol: FFmpeg + ffprobe (ffprobe yoksa `warn`, fallback var) · 5 zorunlu bağımlılık
(faster-whisper, scenedetect, librosa, numpy, imageio-ffmpeg) · 2 opsiyonel (playwright, google-genai) ·
Whisper model önbelleği · depolama yazılabilirliği + boş alan (<500 MB → uyarı) ·
her kimlik bilgisinin `configured`/`missing` durumu · projenin kaynak videosu.

Her `fail` bir **çözüm metni** taşır. Preflight hiçbir secret **değeri** okumaz, hiçbir sağlayıcıya bağlanmaz.
Engel varsa her ekranın üstünde kırmızı banner çıkar; tam liste Ayarlar sekmesinde.

### Eklenen: doğrusal pipeline ilerleme göstergesi

9 adım: kaynak → Faz 0 değerlendirme → plan onayı → analiz → moment seçimi →
*(script onayı → ses onayı, yalnızca `narrated_theory`)* → render → final onay.

Her adım `done` / `current` / `blocked` / `pending` / `skipped`; **tıklanabilir** ve
sahibi olan sekmeye götürür. Üstte yüzde çubuğu, altta "Sıradaki: X" + eylem metni +
"Bu adıma git" düğmesi. `direct_clip_series` seçildiğinde anlatım adımları `skipped`
olarak gösterilir — beklemede değil, ilgisiz.

Sunucu tarafında hesaplanır; tarayıcı hiçbir pipeline kuralını yeniden türetmez.

### Test kanıtı

| Suite | Sonuç |
|---|---|
| `verify_console.py` | Geçti — bölüm 19: preflight kapsamı, çözüm metinleri, secret sızmaması, ilerlemenin **ilerlemesi** (2/7 → 3/7), mod-farkındalı `skipped` |
| `verify_browser.py` | 71/71 — 9 adım rozeti, `4/7 (%57)`, current işaretli, adım tıklaması doğru sekmeye götürüyor, 24 preflight satırı, panelde secret yok |
| `verify_faults.py` | 70/70 (regresyon yok) |
| `verify_local.py` | Geçti (regresyon yok) |
| `verify_real_preview.py` | Geçti — 540×960 h264+aac, 42.77s, `review_required` |

### Kalan blokerler

- Üretilen önizleme **hâlâ insan tarafından izlenmedi** (altyazı senkronu, crop, ses).
- `narrated_theory` yalnızca mock Gemini ile koşuldu; gerçek script/TTS kalitesi bilinmiyor.
- Whisper `small`/`large-v3-turbo` modelleri önbellekte yoksa ilk analiz internet ister (preflight uyarıyor).
- Yalnızca Chromium; Firefox/WebKit ve gerçek mobil cihaz test edilmedi.

## 13f. Render Studio inceleme deneyimi

**Durum:** `verification_required`
**Kanonik modül:** [creator_console/render_review.py](creator_console/render_review.py)
**Artefakt:** `results/render_review.json`

### Eklenen

- **Büyük gömülü oynatıcı** (`max-height: 70vh`), altında boyut/çözünürlük/süre/durum şeridi.
- **Teknik özet** (12 alan): süre, çözünürlük + 9:16 doğrulaması, video codec, ses (orijinal korundu mu), TTS kullanıldı mı, altyazı modu, cue/kelime sayısı, aktif-kelime vurgusu, caption tema/font, render modu, kalite, `artifact_status`.
- **6 maddelik insan inceleme listesi:** klip sınırları · kadraj/crop · altyazı senkronu · aktif kelime vurgusu · font/okunabilirlik · ses sürekliliği. Her madde bir soru ve düzeltmenin yapılacağı sekmeyi taşır.
- **Final onay listesi tamamlanmadan açılmaz.** Onay düğmesi DOM'da bile yok; `PipelineService.approve("render")` de sunucu tarafında reddeder — UI'dan atlatılamaz.
- **"Revizyon gerekli"**: zorunlu not + hedef sekme (Moment Explorer / Render / Faz 0) + doğrudan gitme düğmesi. Videoyu **değiştirmez**, hiçbir durumu onaylamaz.

### Hash bağı

Her işaret, işaretlendiği videonun SHA-256'sını kaydeder — onay kapısının kullandığı **aynı** digest. Video yeniden üretilince liste `stale` olur ve sıfırlanır; kimsenin izlemediği bir video onaylanamaz.

### Test kanıtı

| Suite | Sonuç |
|---|---|
| `verify_console.py` bölüm 20 | 6 madde sözlüğü, teknik özet alanları, **liste eksikken onay HTTP 400 `approval_rejected`**, isimsiz inceleyici reddi, restart sonrası kalıcılık, liste tamamlanınca onay 200, revizyon notu zorunlu, revizyon **videoyu değiştirmiyor**, video değişince 6/6 → 0/6 + onay geçersiz |
| `verify_browser.py` bölüm 15 | Oynatıcı yükleniyor (`readyState`, `duration=6.01s`), 12 teknik alan, 6 kutu, **onay alanı kilitli** → UI'dan 6 kutu işaretlenince **açılıyor**, yenileme sonrası 6/6, revizyon kaydı + doğru sekmeye yönlendirme, revizyon sonrası render `approved` değil |
| `verify_faults.py` | 70/70 (regresyon yok) |
| `verify_local.py` | Geçti (regresyon yok) |
| `verify_real_preview.py` | Geçti — gerçek HOTD render'ı, `review_required` |

Bulunan test kusurları: Türkçe locale `text-transform: uppercase` (`i` → `İ`) `inner_text` karşılaştırmasını bozuyordu → ham `textContent` kullanılıyor; otomasyon testi artık insan adımı olarak kontrol listesini dolduruyor.

### Kalan blokerler

- Kontrol listesi **doğru soruları** soruyor ama cevapları hâlâ insan verecek: gerçek HOTD render'ı izlenmedi.
- `narrated_theory` render'ı için inceleme listesi test edilmedi (yalnızca `direct_clip_series`).
- Yalnızca Chromium.

## 13g. narrated_theory canlı doğrulaması (1 gerçek Gemini çağrısı)

**Durum:** `verification_required` (script üretildi, **insan okuması bekliyor**)
**Betik:** [verify_live_script.py](verify_live_script.py) — açık onay bayrağı olmadan çalışmayı reddeder

### Koşu

| | |
|---|---|
| Model | **`gemini-3.6-flash`** (Rota A birincisi; sabitlendi) |
| Gerçek API çağrısı | **1** `generate_content` — sayaçlı proxy ile ölçüldü, koddan varsayılmadı |
| Kimlik | `GeminiKey` (`.env.local`), değeri hiçbir yerde görünmedi |
| Kaynak | HOTD 1329s, izole `wsp_livetest` workspace'i |
| Moment | rank 1, 63s–106s (yerel analiz, `small` Whisper) |
| Teori açısı | Daemon Targaryen'in tahtı gerçekten istemediği teorisi |
| Script | 587 karakter Türkçe |
| Artefakt | `.console_data_livetest/workspaces/wsp_livetest/projects/prj_807b9e42fcb01b30/results/selected_script.json` |
| Durum | **`review_required`** — onaylanmadı |

### Tek çağrı nasıl garanti edildi

`allow_provider_fallback=False` (OpenAI/Anthropic yok) · `enable_google_model_fallback=False`
(3.5-flash / flash-lite yok) · `max_google_model_attempts=1` · **`max_google_transient_retries=0`**
(5xx/RPM yeniden denemesi yok). Son parametre bu turda eklendi: `script_generation` zincirinde
uçtan uca bağlı değildi, yani "tek çağrı" iddiası doğrulanabilir değildi.

Kayıtlı rota: `route_planned=['gemini-3.6-flash']`, `provider_attempts` 1 kayıt.

### Hard stop'lar doğrulandı

TTS **çalıştırılmadı** · render **çalıştırılmadı** · script **onaylanmadı**
(`human_approval.approved=False`) · yükleme/yayın yok · `production_ready` yok ·
API yanıtında ve DOM'da anahtar yok.

### Script Studio görünürlüğü (gerçek Chromium)

Başlık, teori açısı (`theory-angle`), inceleme hash alanı (`hash-script`), onay düğmesi
(`approve-script`), `review_required` rozeti ve faz durum paneli görünüyor; JS hatası yok;
ilerleme göstergesi `5/9 (%56)`, current adım **"Script onayı"**.

### Kalan blokerler

- **Üretilen script insan tarafından okunmadı/onaylanmadı** — kalite yargısı verilmedi.
- `hook` alanı modelden dönüyor ama artefakta yazılmıyor (`build_selected_script_artifact`
  yalnızca `script_text` saklıyor). Bloklayıcı değil; hook tabanlı kalite kontrolü için gerekli.
- Gemini TTS canlı olarak hâlâ hiç çağrılmadı.
- Bu tek koşu kaliteyi değil, **entegrasyonun çalıştığını** kanıtlar.

## 13h. Yetenek teşhis sistemi (fonksiyonel)

**Durum:** `verification_required`
**Modül:** [creator_console/diagnostics.py](creator_console/diagnostics.py) ·
**API:** `GET /api/diagnostics` (önbellek), `GET ?run=1` / `POST` (çalıştır) ·
**Arayüz:** Ayarlar → "Yetenek teşhisi"

### Neden preflight yetmiyordu

`preflight` "ortam kurulu mu" sorusunu yanıtlar. Bu gerekli ama yeterli değil: her
bağımlılık sorunsuz import edilirken moment seçimi her adayı reddediyor, medya rotası
host yolu sızdırıyor veya inceleme kapısı incelenmemiş bir render'ı geçiriyor olabilir.
Import kontrolü bunların hiçbirini göremez.

### 13 yetenek, hepsi gerçekten çalıştırılarak

| Yetenek | Ne doğrulanıyor |
|---|---|
| `source_upload` | kayıt + parmak izi + göreli yol + düşmanca dosya adı temizleniyor |
| `preflight` | 24 ortam kontrolü |
| `assessment` | öneri + skor üretiliyor **ve kendi kendine mod SEÇMİYOR** |
| `plan` | oluşturma, hash onayı, **plan değişince kapı yeniden kapanıyor** |
| `video_analysis` | aday moment üretiliyor, `review_required` |
| `moment_preview` | preview göreli yolla sunuluyor, bayat seçim yakalanıyor |
| `script` | konsol yolu (erişilebilirlik ≠ işlevsellik, aşağıya bak) |
| `tts` | 24kHz/16bit WAV ses sözleşmesi |
| `render` | gerçek FFmpeg render + teknik doğrulama |
| `review` | **eksik listeyle onay REDDEDİLİYOR**, tamamlanınca açılıyor |
| `automation` | başlat/tick/iptal; plan seçiminde duruyor, mod seçmiyor |
| `persistence` | atomik yazım + restart sonrası durum ve iş geçmişi |
| `media_serving` | namespace içinde sunum, dışarıdaki kanarya dosyası okunamıyor |

Durumlar: `ready` · `degraded` · `blocked` · `not_configured` · `failed`.
Her `ready` olmayan satır **engel** ve **çözüm** metni taşımak zorunda (test edilir).

### Sağlayıcı erişilebilirliği ≠ işlevselliği

Bilinçli olarak ayrı. "Gemini anahtarı yapılandırılmış" ile "Gemini kullanılabilir script
döndürüyor" farklı iddialardır. `CREATOR_CONSOLE_LIVE_DIAGNOSTICS=1` olmadan `script` ve
`tts` **`degraded`** raporlanır ve panel bunun neden beklenen olduğunu açıkça yazar.
Canlı model doğrulaması ayrı bir işin konusudur ([verify_live_script.py](verify_live_script.py)).

### İzolasyon

Her koşu kendi geçici depolama kökünü (`tempfile.mkdtemp`) kurar ve sonunda siler.
Kullanıcının projeleri **ne okunur ne yazılır**; testle doğrulanır: koşudan sonra
kullanıcının ağacında tek yeni dosya `workspaces/<ws>/diagnostics.json` raporudur.
Canlı sağlayıcı çağrılmaz (patlayan istemci enjekte edilerek doğrulanır), hiçbir şey
yüklenmez/yayınlanmaz, secret **değeri** rapora/DOM'a girmez.

Rapor metinleri yazılmadan önce host yollarından temizlenir — engel metni gerçek
istisna mesajlarından üretildiği için geçici dizini taşıyabilir ve bu rapor hem diske
hem tarayıcıya gider.

### Bu tur bulunan ve düzeltilen gerçek sorunlar

1. **Fikstür kendi kaynağını bozuyordu** — düşmanca dosya adı testi paylaşılan projenin
   videosunun üzerine yazıyordu; sonraki 5 yetenek yanlış nedenle `failed` oluyordu.
   Ayrı bir scratch projeye taşındı.
2. **`live_app` / fikstür karışması** — iki kontrol `project_view` için canlı uygulamayı
   kullanıyordu, yani kullanıcının deposuna yazabilirdi. İkinci parametre `live_app`
   olarak adlandırıldı; yalnızca ortam probu ve kimlik **durumu** onu kullanır.
3. **Gezinme testi yanlış nedenle geçiyordu** — `/etc/passwd` uzantısı olmadığı için
   `media_type_blocked` dönüyordu, gezinme engellendiği için değil. Namespace dışına
   servis edilebilir uzantılı bir kanarya dosyası konularak gerçek sızıntı test edildi.
4. **Blocker metnine host yolu sızıyordu** — rapora `_scrub` eklendi.
5. **Kontrol sırası yanlıştı** — mutlak yol kontrolü `resolve` çağrısından sonraydı,
   bu yüzden kesin bulgu yerine depolama istisnası raporlanıyordu.

### Testler

[verify_diagnostics.py](verify_diagnostics.py) — **109 kontrol**. Pozitif yarım
(sağlam ağaçta her yetenek doğru sonuç veriyor) + **10 hata enjeksiyonu**: FFmpeg yok,
plan onayı içeriğe bağlanmıyor, eksik listeyle render onaylanıyor, bayat seçim geçerli
sayılıyor, mutlak yol sızıyor, gezinme engellenmiyor, otomasyon insan kapısını aşıyor,
atomik yazım geçici dosya bırakıyor, kontrolün kendisi çöküyor, değerlendirme kendi
kendine mod seçiyor. Her enjeksiyon, ilgili kontrolün **gerçekten başarısız olabildiğini**
kanıtlayan pozitif kontroldür.

[verify_browser.py](verify_browser.py) bölüm 14 — **22 yeni Chromium kontrolü**:
panel var, sayfa açılışı koşu tetiklemiyor, düğme gerçekten çalıştırıyor, son çalışma
zamanı güncelleniyor, 13 satır durum taşıyor, sağlayıcı `degraded` olarak okunuyor,
engel/çözüm alanları görünüyor, "sekmeye git" doğru sekmeye götürüyor, rapor yeniden
yüklemede kalıcı, DOM'da secret ve fikstür yolu yok.

### Kalan blokerler

- Teşhis, sağlayıcı **işlevselliğini** kanıtlamaz — tasarım gereği. Gemini/TTS canlı
  yolu yalnızca ayrı, açık onaylı koşularla doğrulanır.
- Tam koşu ~10 sn (gerçek FFmpeg + Whisper `tiny`). Uzun kaynaklarda değil, sentetik
  3 sn'lik klip üzerinde çalışır; ölçek davranışını kanıtlamaz.
- YouTube yükleme yeteneği kayıtta **yok**, çünkü henüz uygulanmadı (M1+).

## 13i. Pipeline akış haritası (etkileşimli)

**Durum:** `verification_required`
**Sunucu:** [creator_console/status.py](creator_console/status.py) → `build_workflow_map()` ·
**Arayüz:** her proje ekranının üstünde, eski ilerleme göstergesinin **yerine**

### Akış

`Kaynak → Değerlendirme → Plan → Analiz → Moment → Script → Ses → Render → İnceleme`

Durumlar: `completed` · `current` · `waiting` · `blocked` · `failed` · `skipped`.

### Sunucu türetimli — JavaScript'te kural yok

Hangi adımın `current` olduğu, hangisinin bu modda atlandığı, sıradaki eylem, engel ve
çözüm metni **tamamı** `build_workflow_map()` içinde, pipeline'ın kendi kullandığı
artefaktlar ve onay zincirinden türetilir. Tarayıcı yalnızca renk ve yerleşim seçer.
Bu kuralları JS'te yeniden türetmek, arayüzün tarif ettiği pipeline ile ihtilafa
düşmesinin yoludur.

Görünüm modelinde tek alan: `workflow`. Eski `progress` alanı ve
`progressIndicator()` bileşeni **kaldırıldı** — test ediliyor (`workflow:single-view-field`,
`workflow:no-duplicate-indicator`).

### Mod farkındalığı

`direct_clip_series` → Script ve Ses `skipped`, toplam 7 adım, her atlanan adım
*neden* atlandığını söyler (klipler orijinal sesiyle kesilir).
`narrated_theory` → 9 adımın hepsi aktif.

**Bu turda düzeltilen gerçek hata:** eski gösterge, **plan daha yokken** Script/Ses'i
`skipped` sayıyordu (`mode is None` → `modes` eşleşmiyor). Kullanıcı henüz mod
seçmeden akış 7 adım görünüyor, sonra `narrated_theory` seçilince sessizce 9'a
büyüyordu. Artık atlama yalnızca **mod bilindiğinde** uygulanır.

### Başarısız ve engelli durumlar

`failed` iki kaynaktan gelir: bir fazın **en yeni** işi başarısızsa, veya artefakt
`status: failed` ise. En-yeni-iş kuralı bilinçli: başarısız olup sonra başarıyla
yeniden çalıştırılmış bir faz artık başarısız değildir; onu `failed` göstermek
kullanıcıyı zaten düzelmiş bir şeyi düzeltmeye yollar (`workflow:stale-failure-ignored`).

`blocked` bozuk/okunamayan artefakttan gelir. Her iki durum da engel **ve** çözüm
metni taşır, hem adım üzerinde hem üstteki özet kutusunda.

### Tema ve yerleşim

Turuncu = "şu an buradasın / eylem burada". Durum renkleri (yeşil/sarı/kırmızı)
turuncudan ayrı tutulur, böylece "sıradaki adımın" ile "bu başarısız oldu" karışamaz —
test ediliyor (`workflow:failed-not-orange`). Her durum ayrıca metin etiketi ve işaret
taşır; renk hiçbir zaman tek sinyal değildir.

Masaüstü: bağlayıcılı yatay akış. ≤900px: **aynı markup** dikey listeye akar (ikinci
bileşen yok). Mobilde yatay taşma ölçülerek 0px doğrulanır — hem sayfa hem haritanın
kendi kutusu.

### Testler

[verify_console.py](verify_console.py) bölüm 19 — **28 sunucu kontrolü**: adım sırası,
durum sözlüğü, tam olarak bir `current`, her adımın sekmesi, mod seçilmeden atlama
yapılmaması, `direct_clip_series` 7 / `narrated_theory` 9 adım, iş başarısızlığının
yüzeye çıkması, bayat başarısızlığın yok sayılması, bozuk artefaktın `blocked` olması,
restart sonrası aynı kalması, görünüm modelinde tek alan olması.

[verify_browser.py](verify_browser.py) bölüm 13 + 13b — **57 Chromium kontrolü**:
9 adım doğru sırayla, `current` vurgusu ve `aria-current`, turuncu tema değeri ölçülü,
adıma tıklayınca doğru sekme, "Bu adıma git", iki mod ayrı projelerde, gerçek başarısız
iş → `failed` + engel + çözüm, bozuk artefakt → `blocked`, yenileme ve **sunucu
restart'ı** sonrası aynı durumlar, mobilde `flex-direction: column` + **0px taşma** +
dokunarak navigasyon.

Toplam: `verify_browser.py` 107 → **142 kontrol**.

### Kalan blokerler

- Harita **onay vermez ve faz çalıştırmaz** — yalnızca gezinme ve durum gösterir.
  Onay kapıları ve hash doğrulaması olduğu gibi korunmuştur.
- `failed` durumu iş kayıtlarından okunur; iş kaydı olmadan çalıştırılan bir faz
  (ör. doğrudan servis çağrısı) yalnızca artefakt durumundan görünür.
- Adım etiketleri sabit Türkçe; yerelleştirme yok.

## 13j. Zorunlu alan doğrulaması ve faz ilerleme panelleri

**Durum:** `verification_required`
**Yeni:** [creator_console/progress.py](creator_console/progress.py) ·
[verify_forms_progress.py](verify_forms_progress.py)

### Zorunlu alanlar

Tek bir paylaşılan alan kurucusu (`field` / `inputField` / `textareaField`) turuncu
yıldızı, `aria-required="true"` bayrağını, "(Opsiyonel)" etiketini ve satır içi hata
yuvasını **birlikte** üretir — bunlar dört ayrı süs değil, tek bir sözleşme. `validateFields`
tüm hataları aynı anda gösterir ve **ilk geçersiz alana odaklanır**. Arayüzde artık elle
kurulmuş tek bir `.field` kalmadı (test ediliyor: `no-raw-fields`).

**Sunucu yetkili kalır.** İstemci doğrulaması bir nezaket; asıl reddi `api.py` içindeki
`_required` yapar ve hangi alanın eksik olduğunu **kapalı bir kümeden** (`FIELD_LABELS`)
isimlendirir. Yanıt gövdesi hiçbir zaman kullanıcı **değerini** yankılamaz — test ediliyor.

#### Bu turda düzeltilen iki gerçek sessiz-varsayılan hatası

1. **Plan imzası uyduruluyordu.** `POST /api/projects/:id/plan` içinde
   `selected_by=payload.get("selected_by") or "console"` vardı. Pipeline mod seçimi için
   isimli bir insan şart koşarken API, kullanıcı adını boş bıraktığında planı sessizce
   `"console"` diye imzalıyordu — onay kapısının var olma sebebi olan hesap verebilirliği
   tam olarak sahteliyordu. Artık 400 + `selected_by` alan adı dönüyor ve plan
   **oluşturulmuyor**.
2. **Sayısal alanlar sessizce sıfırlanıyordu.** `Number(...) || null`, kullanıcının yazdığı
   `0` gibi bir değeri de `null`'a çeviriyordu. Artık boş alan `null` (sunucu önerisi
   uygulanır, belgeli davranış), dolu ama geçersiz alan istemcide satır içi hata.

Ayrıca `POST /api/projects` artık boş başlık kabul etmiyor.

#### Düzeltilen üçüncü hata (arayüz)

`reviewerId` sabiti `renderReviewChecklist()` içinde tanımlıyken `renderRevisionPanel()`
tarafından kullanılıyordu → "Revizyon iste" düğmesi tıklandığında `ReferenceError`.
Tarayıcı testi yakaladı; sabit `REVIEWER_INPUT_ID` olarak modül seviyesine taşındı.

### Faz ilerleme panelleri

Sunucuda kalıcı, aktivite başına tek panel: **upload · diagnostics · assessment ·
analysis · script · TTS · render · retry · automation**. Her kayıt `step`, `completed`,
`total`, `percent`, `message`, `elapsed_sec`, `updated_at` taşır ve
`results/../progress/<aktivite>.json` içinde saklanır.

| Faz | Aşamalar |
|---|---|
| Analiz | preparation · audio · whisper · scenes · scoring · previews |
| Render | preparation · subtitles · ffmpeg · validation · manifest |
| Değerlendirme | probe · scoring · write |
| Script | preparation · request · parse · write |
| TTS | preparation · synthesize · validate · write |
| Yükleme | receive · store · fingerprint |
| Teşhis | 13 yeteneğin her biri bir aşama (gerçekten sayılabilir) |
| Otomasyon | akış haritasının atlanmayan adımları |

**Sahte yüzde yok.** `determinate` yalnızca sayılabilir bir şey sayıldığında `true`:
tamamlanan aşama / toplam aşama, veya ölçülmüş bir öğe sayısı (üretilen önizleme klipleri,
yazılan altyazı satırları). Ölçülemeyen her durumda bar **belirsiz** çizilir ve
`aria-valuenow` **hiç verilmez** — yardımcı teknoloji bunu "bilinmiyor" diye okur, ki doğrusu
budur. Hiçbir kod yolu ara değer uydurmaz; test ediliyor (`prog:no-fake-valuenow`,
`no-stages-indeterminate`, her kayıt için `percent-derived`).

`local_pipeline` tarafına yalnızca **opsiyonel** bir `progress` gözlemcisi eklendi
(`run_video_analysis`, `run_render_pipeline`, `run_original_audio_subtitle_pipeline`,
`generate_preview_clips`). Notebook çağrı yolları hiçbir şey geçmez ve aynen eskisi gibi
davranır.

**Durumlar:** `running` · `completed` · `failed` · `interrupted` · `stopped` · `idle`.
`stopped` bilinçli olarak `failed`'dan ayrı: otomatik akış bir insan kapısında beklerken
bunu kırmızı boyamak kullanıcıya kırmızıyı yok saymayı öğretir.

**Kalıcılık:** sayfa yenilemesi, ikinci sekme ve **sunucu restart'ı** aynı barı aynı aşamada
gösterir. Ölen bir işçinin kaydı iş kurtarma sırasında `interrupted` işaretlenir — `running`
bırakılsa sonsuza kadar dönerdi, silinse işin kaybolduğu gizlenirdi. Tamamlanmış bir kaydın
üzerine geç gelen kurtarma yazamaz.

**Hata:** son adım, hata metni ve yeniden deneme eylemi gösterilir; yeniden deneme aynı iş
retry uç noktasını kullanır (ikinci bir çalıştırma yolu yok) ve `attempt` numarası panelde
"2. deneme" olarak görünür.

**Kopya panel yok:** `phaseProgress(activity)` her render geçişinde bir aktiviteyi yalnızca
bir kez çizer (`renderedProgress` kümesi), bileşenler nasıl birleştirilirse birleştirilsin.

**Erişilebilirlik:** `role="progressbar"`, ölçülebilirken `aria-valuemin/max/now`,
her zaman `aria-valuetext` ve `aria-busy`.

**Mobil:** aşama listesi dikey yığılır, panel 390px'e sığar, yatay taşma 0px.

### Testler

[verify_forms_progress.py](verify_forms_progress.py) — **80 API kontrolü**: zorunlu alan
reddi ve alan adı, isimsiz plan oluşturulmaması, değer yankılanmaması, aşama listeleri,
yüzde türetimi, dürüstlük (`determinate=false` iken `percent=None`), hata kaydı, yeniden
deneme sayacı, restart, ölen işçi, `stopped ≠ failed`, arayüz sözleşmesi.

[verify_browser.py](verify_browser.py) bölüm 17 + 18 — **44 yeni Chromium kontrolü**:
yıldızın gerçekten turuncu olduğu (hesaplanmış renk), `aria-required`, boş formda hiçbir şey
oluşturulmaması, satır içi hata + ilk geçersiz alana odak, opsiyonel etiketi, yıldız ile
opsiyonelin asla aynı alanda olmaması, formu atlayarak sunucunun yine reddetmesi,
sunucunun isimlendirdiği alanın DOM'da işaretlenmesi, aşama adları, `progressbar`
nitelikleri, belirsiz barda `aria-valuenow` olmaması, hata bloğu, yenileme ve **restart**
sonrası korunma, mobilde 0px taşma.

Toplam: `verify_browser.py` 142 → **186 kontrol**.

### Kalan blokerler

- İlerleme **aşama** çözünürlüğünde; FFmpeg'in kendi kare ilerlemesi okunmuyor, bu yüzden
  uzun bir kodlama aşaması tek bir "çalışıyor" olarak görünür.
- Kalan süre tahmini **yok** — bilinçli: elde geçmiş süre verisi olmadan verilecek her ETA
  uydurma olurdu.
- `stale` bayrağı 180 sn güncelleme yokluğunda konur; bundan kısa donmalar ayırt edilmez.
- Teşhis ilerlemesi workspace seviyesinde saklanır (proje içinde değil), çünkü teşhis bir
  projeye ait değildir.

## 13k. Script okunabilirliği ve ölçülen FFmpeg ilerlemesi

**Durum:** `verification_required`

### Onaylanan script artık okunabiliyor

Script Studio bugüne kadar yalnızca **onay panelini** gösteriyordu: inceleyen kişi
okuyamadığı bir metnin SHA-256'sını onaylıyordu. Bu bir inceleme değil. Artık üretilen
script, onay panelinin hemen üstünde tam metin olarak görünüyor (`script-text`), hook
alıntı olarak vurgulanıyor (`script-hook`), yanında model/sağlayıcı/dil/karakter/tahmini
süre künyesi var.

Görünüm modeline eklenen `script` alanı **seçilmiş bir dilim** — yönlendiricinin
deneme-deneme tanılaması (`provider_attempts`) yazının kalitesine karar veren kişinin
önüne dökülmüyor; o İş akışı ekranına ait. Panel salt-okunur: düzenleme, onayın bağlı
olduğu hash'i geçersiz kılardı.

### `hook` alanı artık kaybolmuyor

Prompt modelden `script`, `hook` ve `estimated_duration_sec` istiyordu; **her** sağlayıcı
yolu (`openai`, `anthropic`, `google` sabit model, `google` tam Rota A, çapraz-sağlayıcı
yedek) yalnızca `parsed["script"]`'i alıp diğer ikisini yere bırakıyordu. Yeni
`script_extras()` bunları normalize edip taşıyor; `build_selected_script_artifact` artık
`hook` ve `estimated_duration_sec` yazıyor.

**Uydurma yok:** model hook döndürmediyse alan `None` kalır ve arayüz bunu açıkça
söyler ("Model bu yanitta ayri bir hook cumlesi dondurmedi") — script metninden hook
türetilmez.

### FFmpeg kodlaması artık gerçekten ölçülüyor

`run_ffmpeg`, `progress` gözlemcisi **ve** bilinen çıktı süresi verildiğinde ffmpeg'in
kendi `-progress pipe:1` akışını okuyup kodlanmış saniyeyi toplam saniyeye karşı
raporluyor. Bu bir tahmin değil ölçüm: ffmpeg ne kadar çıktı yazdığını söylüyor, toplam
uzunluk çağrıdan önce biliniyor. Her iki render hattının **son kodlama** çağrısına bağlı.

Gözlemci verilmediğinde eski tek-atışlık yol aynen çalışır, yani notebook çağrı yerleri
değişmedi (test ediliyor: `ffmpeg-unmeasured-path-intact`). Başarısız bir ölçülü koşu yine
ffmpeg'in kendi stderr'iyle `RuntimeError` atar; stderr ayrı bir iş parçacığında boşaltılır
çünkü dolan bir boru okuyucuyu kilitleyebilirdi.

Arayüzde bunun için **ikinci, ayrı** bir çubuk var (`progress-inner-<aktivite>`), yalnızca
çalışan aşamanın kendi sayacı varken çizilir. Aşama çubuğuyla **birleştirilmedi**:
aşamalar eşit sürmediği için karıştırmak hiçbir şeyin ölçümü olmayan bir sayı üretirdi.

### Testler

[verify_forms_progress.py](verify_forms_progress.py) bölüm 10-11 — **25 yeni kontrol**:
ffmpeg ilerlemesinin monoton olması, toplamı aşmaması, tam toplamda bitmesi, birimin `sn`
olması, gözlemcisiz yolun bozulmamış olması, başarısızlığın stderr'i koruması;
`script_extras` normalizasyonu, olmayan alanın uydurulmaması, hook'un artefakta ve
görünüme taşınması, görünümün seçilmiş dilim olması, hook'suz yanıtta `None` kalması.

[verify_browser.py](verify_browser.py) bölüm 19 — **12 yeni Chromium kontrolü**: script
metninin sayfada olması ve **artefaktla birebir eşleşmesi**, hook'un görünmesi, künye,
onay hash'inin aynı ekranda olması, ham artefaktın sızmaması, script yokken boş-durum
mesajı, iç çubuğun **yalnızca ölçüm varken** çizilmesi ve `aria-valuenow` değerinin doğru
olması.

Toplam: `verify_browser.py` 186 → **198**, `verify_forms_progress.py` 80 → **105**.

### Kalan blokerler

- Ölçüm **son kodlama** çağrısında; ondan önceki temel klip kesme ve Whisper altyazı
  çıkarımı hâlâ aşama çözünürlüğünde.
- Kalan süre tahmini yok (bilinçli).
- Gemini TTS canlı olarak hâlâ hiç çağrılmadı.

## 13l. Yerel entegrasyon ve UX kabul geçişi

**Durum:** `verification_required` (otomatik kabul geçti; **insan izlemesi bekliyor**)
**Betik:** [verify_acceptance.py](verify_acceptance.py) — **109/109**

### Ne yapıyor

Diğer paketler her biri bir şeyi iyi kanıtlıyor. Bu geçiş onların soramadığı tek soruyu
soruyor: bir insan, **yalnızca tarayıcıyı kullanarak**, kod okumadan, gizli adım olmadan
ve çıkışı olmayan bir çıkmaza düşmeden "elimde bir video var"dan "onayımı bekleyen bir
Short var"a gidebiliyor mu?

Gerçek Chromium, gerçek `ConsoleServer`, **gerçek HOTD kaynağı** (204 MB) ve izole bir
depolama kökü üzerinde tüm ürünü sırayla yürüyor: boş konsol → preflight → teşhis →
zorunlu alan doğrulaması → akış haritası → kaynak yükleme → Faz 0 (değerlendirme, plan
oluştur/düzenle/onayla) → manuel analiz + canlı ilerleme → yenileme → moment önizleme
ve seçimi → otomatik akış → Direct Clip Series render → **gerçek süreç ölümüyle** backend
restart → inceleme listesi → revizyon isteği → hard stop denetimi → mobil.

**Güvenlik:** sağlayıcılar sahte (canlı Gemini/TTS/YouTube çağrısı **yok**), kullanıcının
kendi projeleri ne okundu ne yazıldı, hiçbir şey yüklenmedi/yayınlanmadı ve **final onay
verilmedi** — koşu `review_required`'da duruyor.

İşler **thread** koşucusunda çalışıyor, inline değil: bu geçişin kontrol ettiklerinin
yarısı (hareket eden çubuk, iş sırasında yenileme, iş sırasında restart) yalnızca iş
gerçekten uçuştayken var olur.

### Bulunan ve düzeltilen gerçek hata

**İnceleyen kişi adı her yeniden çizimde siliniyordu.** Kontrol listesindeki her işaret
bir API çağrısı, her çağrı bir yeniden çizim, her yeniden çizim boş bir input → 6 maddeyi
işaretlemek için adı **6 kez** yazmak gerekiyordu ve 2-6. maddeler
"inceleyen kisi zorunlu" ile 400 dönüyordu.

Bu hata daha önce görünmedi çünkü [verify_browser.py](verify_browser.py) her tıklamadan
**önce** adı yeniden dolduruyordu — testin kendisi hatayı barındırıyordu. Geçici çözüm
kaldırıldı ve yerine adın yeniden çizimden sağ çıktığını doğrulayan bir kontrol kondu;
ad artık `state.renderReviewer`'da tutuluyor (sonuç yine sunucuda, bu yalnızca input
değerinin bir repaint'i atlatması).

**Ayrıca:** iş tablosundaki "Yeniden dene" düğmesinin test kancası yoktu — kurtarılan bir
işin yeniden denenebildiğini arayüzde doğrulamak için eklendi.

### Testin kendisinde düzeltilen iki yanlış varsayım

1. **Restart aynı süreçte simüle ediliyordu.** Kurtarma sahip-farkında: sahip PID canlıyken
   işi almaması **doğru** davranıştır, dolayısıyla test yanlış bir şeyi iddia ediyordu.
   Artık gerçek bir çocuk süreç işi claim edip **öldürülüyor**; ayrıca canlı sahibin işinin
   çalınmadığı da ayrıca doğrulanıyor.
2. `can_retry` saklanan bir alan değil, **görünüm** alanı — arayüzün okuduğu yerden okunuyor.

### Doğrulananlar

Boş konsolda her ekran ne yapılacağını söylüyor · preflight 23 kontrol temiz · teşhis 13
yetenek, bozuk yok · zorunlu alan boş formda hiçbir şey oluşturmuyor ve ilk geçersiz alana
odaklanıyor · akış haritası 9 adım, tıklama doğru sekmeye götürüyor · 204 MB gerçek kaynak
yükleniyor ve parmak izi çıkarılıyor · değerlendirme **kendi kendine mod seçmiyor** ·
isimsiz plan reddediliyor · plan düzenlemesi uygulanıyor · **kapı onaydan önce kapalı,
sonra açık** · analizde 4 farklı aşama canlı gözlendi · yenileme ilerlemeyi koruyor ve
**ikinci iş yaratmıyor** · moment önizlemesi tarayıcıda **oynuyor** (33.2 sn, readyState 4)
· otomatik akış `render_approval` insan kapısında duruyor ve **asla onaylamıyor** · render
5 aşamayı tamamlıyor, teknik doğrulamayı geçiyor, tarayıcıda **540x960 dikey** oynuyor ·
ölü sahibin işi kurtarılıyor, nedeni yazılıyor, arayüzde yeniden deneme sunuluyor ·
artefaktlar restart'tan sağ çıkıyor · liste eksikken onay kapalı ve **nedeni yazılı** ·
revizyon kaydediliyor ve onay sayılmıyor · secret ne API'de ne diskte · 10 ekranın hepsi
render oluyor · 7 ekran mobilde **0px taşma** · JS hatası yok.

### Çıktılar (izole, onaylanmadı)

```
.console_data_acceptance/workspaces/wsp_accept/projects/<pid>/results/
  moment_previews/hot_moment_01.mp4     (33.2 sn önizleme)
  render/final_short.mp4                (1.8 MB, 540x960, review_required)
```

### Kalan insan kontrolleri

Bu geçiş **mekaniğin** çalıştığını kanıtlar, **kalitenin** değil. İnsan gözüyle bakılması
gerekenler: render edilen Short gerçekten izlenebilir mi (kadraj, kesim noktası, altyazı
senkronu, aktif kelime vurgusu, font okunabilirliği, ses sürekliliği — yani inceleme
listesinin kendisi), seçilen momentin gerçekten ilgi çekici olup olmadığı ve Türkçe
metinlerin tonunun uygunluğu.

## 13m. Canlı Gemini TTS doğrulaması (1 gerçek çağrı)

**Durum:** `verification_required` (ses üretildi, **insan dinlemesi bekliyor**)
**Betik:** [verify_live_tts.py](verify_live_tts.py) — `verify_live_script.py` ile aynı desen

### Çift koruma

Normal test koşuları **asla** API çağırmaz. İki bağımsız kapı var:

| Durum | Sonuç |
|---|---|
| `CREATOR_CONSOLE_LIVE_TTS` ayarlı değil | çıkış 0, `SKIPPED`, **çağrı yok** |
| Env var var, `--i-authorize-one-live-call` yok | çıkış 2, `REFUSED`, **çağrı yok** |
| İkisi de var | tam olarak 1 çağrı |

Her ikisi de ayrıca doğrulandı.

### Koşu

| | |
|---|---|
| Model | **`gemini-3.1-flash-tts-preview`** (Rota D birincisi; mevcut yönlendirme değiştirilmedi) |
| Ses / dil | `Kore` · `tr-TR` (konsolun kendi varsayılanı) |
| Gerçek API çağrısı | **1** `generate_content` — sayaçlı proxy ile ölçüldü |
| Test metni | 104 karakter, sabit Türkçe |
| Süre | **8.44 sn** |
| Biçim | 24000 Hz · 1 kanal · 16 bit PCM · 405.164 bayt |
| Artefakt | `.console_data_livetts/.../results/tts/gemini_tts.wav` |
| Durum | **`review_required`** — onaylanmadı |

### Tek çağrı nasıl garanti edildi

`enable_model_fallback=False` (2.5-flash-preview-tts atlaması yok) ·
`max_model_attempts=1` · `max_transient_retries=0`. Bu üç parametre bu turda
`PipelineService.run_tts`'e uçtan uca bağlandı — `run_script_generation` ile aynı desende,
varsayılanlar normal Rota D davranışını koruyor.

### Kapılar aşılmadı, karşılandı

TTS onaylı bir `narrated_theory` planı **ve** onaylı bir script ister. İkisi de
bypass edilmedi: izole çalışma alanında küçük sentetik bir klip kaydedilip yerel
değerlendirme çalıştırıldı, plan oluşturulup **hash ile** onaylandı, script ise
konsolun kullandığı **aynı** `approval.approve_artifact` fonksiyonundan geçirilen bir
test fikstürü. Kullanıcının kendi script/plan artefaktları ne okundu ne yazıldı ne onaylandı.

### Doğrulananlar

Dosya var · WAV konteyner (RIFF/WAVE) · PCM s16le codec · 24000 Hz örnekleme ·
1 kanal · süre makul aralıkta (8.44 sn) · boş değil (405 KB) ·
**script-hash bağlanması** (`source_script_sha256` metnin SHA-256'sıyla birebir,
`source_script_hash` 16 karakterlik kısa biçimi, `source_script_status=approved`) ·
hash'in içeriğe duyarlı olduğu ayrıca kanıtlandı.

**Hard stop'lar:** ses onaylanmadı · render çalıştırılmadı · yükleme/yayın yok ·
`production_ready` yok · anahtar ne API yanıtında ne artefaktta · **ses verisi JSON'a
gömülmedi** (yalnızca WAV dosyasında).

Sağlayıcı hatası durumunda betik durur ve `sanitize_google_error` ile temizlenmiş
mesajı raporlar — ham istisna hiçbir yere yazılmaz.

### Kalan bloker

**Üretilen ses insan tarafından dinlenmedi.** Bu koşu Rota D entegrasyonunun çalıştığını
kanıtlar (gerçek model gerçek WAV döndürüyor, doğrulayıcı ve onay kapısı kabul ediyor);
telaffuz, tonlama ve Türkçe doğallığı hakkında hiçbir şey söylemez.

## 13n. Voice Studio inceleme deneyimi

**Durum:** `verification_required` (mekanik doğrulandı; **insan dinlemesi bekliyor**)
**Yeni:** [creator_console/media_review.py](creator_console/media_review.py) ·
[creator_console/voice_review.py](creator_console/voice_review.py) ·
[verify_voice_review.py](verify_voice_review.py) — **85/85**

### Önce mevcut işlevsellik incelendi

`render_review.py` bu problemi zaten bir kez çözmüştü: hash kapısı inceleyenin *bir şeyler*
gördüğünü kanıtlar, ama pipeline'da gerçekten bozulan şeyleri kontrol ettiğini kanıtlamaz.
Ses için ihtiyaç duyulan **tam olarak aynı kurallar**, farklı sorularla.

Modülü kopyalamak "bu madde bayat mı?" sorusunun iki ayrı uygulamasının zamanla ayrışması
demek olurdu — ilk hata da muhtemelen birinin *hash değişimi tüm listeyi geçersiz kılar,
tek maddeyi değil* kuralını unutması olurdu. Bu yüzden kurallar
[media_review.py](creator_console/media_review.py) içinde **bir kez** yaşıyor, bir
`ReviewSpec` ile parametrelendirilmiş; `render_review` ve `voice_review` yalnızca kendi
sorularını, artefakt yolunu ve düzeltme sekmelerini veriyor.

Arayüz tarafında da aynısı: `reviewChecklist(kind)`, `reviewRevisionPanel(kind)` ve
`reviewApprovalSection(kind, …)` ortak; render panelleri artık bunlara delege eden üç
satır. Testler tek uygulama olduğunu doğruluyor (`single-checklist-implementation`,
`single-approval-gate`).

### Bulunan ve düzeltilen gerçek hata (render'da gizliydi)

**"Revizyonu kapat" hiçbir şey yapmıyordu.** `clear_revision` kaydı `resolved: True`
işaretliyor ama `evaluate` bu bayrağı **hiç filtrelemiyordu** — verdict sonsuza kadar
`needs_revision` kalıyor, dolayısıyla final onay kalıcı olarak bloke oluyordu. Ortak
çekirdek bunu düzeltti ve her iki medyum için de test edildi
(`render-revision-clears`, `voice-revision-clears`).

**İkinci hata (bu turda, kendi kodumda):** Voice Studio ilk halinde onay panelini koşulsuz
çiziyordu — liste eksikken "Sesi onayla" düğmesi görünüyordu. Tarayıcı testi yakaladı;
kapı kararı ortak bileşene taşındı, böylece bir ekranın onu unutması artık mümkün değil.

### Ses inceleme listesi (6 madde)

`pronunciation` · `naturalness` · `pacing` · `tone` · `script_match` · `audio_quality`

Her biri bir insan kulağının karar verdiği ve hiçbir doğrulayıcının duyamayacağı bir şey.
`validate_audio_file` WAV'ın 24 kHz mono PCM olduğunu kanıtlar; yanlış okunan bir özel
ismi duyamaz. Her maddenin kendi sorusu, düzeltme ipucu ve düzeltmenin yapılacağı sekmesi
var (`voice` veya `script` — render sekmesi ses revizyonu için **reddediliyor**).

### Voice Studio ekranı

**Güvenli gömülü oynatıcı:** kaynak, sahiplik denetimli medya rotasından ve
**namespace-göreli** yolla verilir; tarayıcı bir host yolu ne alır ne isteyebilir. Ses
hash'i sorgu dizesinde, böylece yeniden üretilen dosya önbelleği patlatır ve dünkü ses
bugünkü hash altında çalmaz.

**Teknik künye:** ses, model, dil, süre, biçim, örnekleme, kanal, bit derinliği, boyut,
anlatım yönergesi, teknik doğrulama, `artifact_status` ve **bağlı script hash'i**.
Hash eşleşmesi **üç değerli**: eşleşiyor / ESLESMIYOR / karşılaştırılamadı — "bilinmiyor"
asla "eşleşiyor" diye gösterilmez.

**Seslendirilen script** tam metin olarak, dinlerken takip edilebilsin diye — `script_match`
maddesi ancak böyle hafızadan değil karşılaştırarak yanıtlanabilir.

**Yeniden üretme eylemi** aynı manuel iş gönderimini kullanır; seslendirme üretmenin ikinci
bir yolu yoktur.

### Kalıcılık

Liste **artefakt** (`results/voice_review.json`), yani yenileme ve restart'tan sağ çıkar.
İnceleyen adı üç katmanlı: oturum içi `state.reviewers` (yeniden çizim), `localStorage`
(yenileme), ve son maddeyi işaretleyenin adı (restart ve farklı tarayıcı — sunucudan gelir).

### Hash bağlanması

İnceleme, onay kapısının bağlandığı **aynı** digest'e bağlı
(`approval.content_hash_for("voiceover", …)`); test ediliyor. Ses değişince liste
sıfırlanır, 6 madde `stale` olur, verdict `pending`'e döner, onay geçersizleşir ve
yeniden onay **reddedilir** — hepsi test ediliyor.

### Testler

[verify_voice_review.py](verify_voice_review.py) — **85 kontrol**, `verify_live_tts.py`
tarafından üretilen **gerçek Gemini WAV'ının kopyası** üzerinde (orijinal yalnızca okunur).
Ortak çekirdek, teknik künye, hash bağı, onay kapısı, bayatlama, revizyon (isim ve not
zorunlu, hedef sekme kısıtlı), restart, sağlayıcıya hiç dokunulmaması, arayüz sözleşmesi.

[verify_browser.py](verify_browser.py) bölüm 20 — **29 yeni Chromium kontrolü**: oynatıcı
medya rotasını kullanıyor ve gerçekten **çalıyor**, künye alanları, script metni artefaktla
birebir, liste eksikken onay yok, ad tek seferde yazılıp repaint'ten sağ çıkıyor, yenilemede
korunuyor, ses değişince bayatlıyor ve onay yeniden kapanıyor, revizyon kaydediliyor,
doğru sekmeye götürüyor ve **kapatılabiliyor**.

Toplam: `verify_browser.py` 199 → **228**.

### Kalan insan kontrolleri

Mekanik doğrulandı; **kalite doğrulanmadı**. Üretilen sesi dinleyip listeyi doldurmak
gerekiyor: telaffuz (özel isimler), Türkçe doğallığı, tempo, ton, script ile birebirlik,
ses kalitesi. Ses dosyası
`.console_data_livetts/.../results/tts/gemini_tts.wav` yolunda duruyor.

## 13o. İçerik dili: uçtan uca narrated_theory ayarı

**Durum:** `verification_required` (İngilizce ses üretildi, **insan dinlemesi bekliyor**)
**Yeni:** [verify_content_language.py](verify_content_language.py) — **47/47**

### Neden tek bir ayar değil

"İngilizce üret" tek yerde tek ayar değildir; **dört devir teslimi** atlatması gerekir:
plan → script prompt'u → TTS isteği → ses inceleme listesi. Faz 0'da seçilip TTS'e hiç
ulaşmayan bir dil, **İngilizce script'in Türkçe seslendirilmesini** üretir ve pipeline
bunu fark etmez, çünkü her artefakt kendi içinde geçerli görünür.

### Dil akışı

| Aşama | Ne oluyor |
|---|---|
| Faz 0 planı | `decision.content_language` — **zorunlu**, kapalı küme (`en-US`, `tr-TR`), narrated varsayılanı `en-US` |
| Onay hash'i | Dil **karar bloğunda**, yani plan onay hash'inin parçası |
| Script prompt | Dil kuralı **ilk cümlede ve hedef dilde**: "Write the script and the hook in natural, idiomatic American English." Sona eklenen bir "bu arada İngilizce yaz" tam olarak modelin yarım uyduğu şeydir — İngilizce script üstünde Türkçe hook üretir |
| Script artefaktı | `content_language: "en-US"` kaydedilir |
| TTS | `language_code` **plandan** alınır; çağıranın varsayılanı **ezilir** (test: `plan-wins-over-caller`) |
| Ses artefaktı | `language_code: "en-US"` |
| Ses incelemesi | `naturalness` maddesi **"English naturalness"** olarak yeniden etiketlenir |

### Dil değişimi

Dil karar bloğunda olduğu için değiştirmek plan hash'ini değiştirir → mevcut onay zinciri
boyunca **plan, script, ses ve render onayları düşer**. Test ediliyor.

**Hiçbir şey çevrilmez.** Mevcut script metni ve ses dosyası **bit bit aynı kalır**, yalnızca
onaylı olmaktan çıkar (`script-not-translated`, `audio-not-regenerated`). Voice Studio
bunu açıkça söyler: *"Ses plandaki dilde DEĞİL — plan tr-TR, ses en-US. Mevcut ses otomatik
çevrilmez; seslendirmeyi yeniden üret."*

Yeniden etiketleme **işaretleri silmez**: soru yeniden yazılmıştır ama ses dosyası aynıdır,
dolayısıyla digest bağı korunur (`ticks-survive-relabel`).

### direct_clip_series değişmedi

Orijinal ses korunduğu için içerik dili **yoktur**: karar bloğunda alan `None` kalır, açıkça
istense bile eklenmez, ve bir dil taşıyan `direct_clip_series` planı **geçersiz** sayılır.
Bu önemli çünkü karar bloğuna sızacak fazladan bir alan, anlatım hiç kullanmamış planların
onay hash'ini değiştirip geçersiz kılardı.

### Canlı doğrulama (İngilizce)

| | |
|---|---|
| Gerçek API çağrısı | **1** `generate_content` (sayaçlı proxy ile ölçüldü) |
| Model | `gemini-3.1-flash-tts-preview` (Rota D birincisi, fallback KAPALI, retry 0) |
| Ses / dil | `Kore` · **`en-US`** — plandan alındı, hardcode değil |
| Metin | 121 karakter İngilizce |
| Süre | **9.00 sn** · 24000 Hz · 1 kanal · 16 bit PCM · 432.044 bayt |
| Durum | **`review_required`** — onaylanmadı |

### Testler

[verify_content_language.py](verify_content_language.py) — 47 kontrol: kapalı küme,
karar hash'i, desteklenmeyen dilin reddi, `direct_clip_series` bağışıklığı, prompt'ta dil
kuralının **ilk** olması, dört devir teslimin her biri, çağıranın varsayılanının ezilmesi,
dil değişiminde onayların düşmesi ve içeriğin çevrilmemesi, işaretlerin korunması, arayüz.

[verify_browser.py](verify_browser.py) — **+6 kontrol** (228 → **234**): dil seçicisi
yalnızca narrated'da, varsayılan `en-US`, `aria-required`, teknik künyede `en-US`,
listede **"English naturalness"**, Türkçe soru yok.

### Kalan bloker

**Üretilen İngilizce ses dinlenmedi.** Mekanik doğrulandı — dil dört devir teslimi de
atlatıyor, gerçek model gerçek WAV döndürüyor. Telaffuz, İngilizce doğallığı, tempo ve ton
hakkında hiçbir şey söylenmiş değil; o yargı insanın.

## 13p. `deep_storyteller` anlatım ön ayarı

**Durum:** `verification_required` (ses üretildi, **insan dinlemesi bekliyor**)
**Yeni:** [local_pipeline/narration_presets.py](local_pipeline/narration_presets.py)

### Seçilen ses: `Charon` — uydurulmadı, gerekçelendirildi

Ses kimliği **mevcut kayıttan** seçildi ve karar yazılı olarak saklanıyor. Resmî Gemini
TTS ses tablosu canlı dokümandan doğrulandı; depodaki 30 sesin listesi birebir aynı.

| Aday | Belgelenen karakteristik | Karar |
|---|---|---|
| **Charon** | **Informative** | **Seçildi** — ölçülü, açıklayıcı yetişkin erkek anlatım; kontrollü sinematik hikâye anlatıcısının ihtiyacı bu |
| Gacrux | Mature | Runner-up; "olgun" ama belgelenmiş bir derinlik/anlatı niteliği yok |
| Algenib | Gravelly | Doğallığı dokuya feda ediyor |
| Fenrir | Excitable | Bu ön ayarın **kaçınmak için var olduğu** fragman enerjisi |
| Kore / Orus / Alnilam | Firm | Derin değil, iddialı okunuyor |

`validate_presets()` bir ön ayar API'nin desteklemediği bir ses adlandırırsa yüksek sesle
patlar — uydurulmuş bir ses kimliği testle engelleniyor.

### Neden ön ayar, iki ayrı alan değil

Ses ve anlatım yönergesi yalnızca **birlikte** anlamlıdır. Heyecanlı bir fragman
yönergesiyle `Charon` kimsenin istediği derin anlatıcı değildir; dikkatli bir yönergeye
iliştirilmiş `Fenrir` (*Excitable*) sesi şekillendirmek yerine onunla kavga eder. Tek bir
ad altında eşleştirmek seçimi incelenebilir, saklanabilir ve koşular arasında
karşılaştırılabilir kılar.

### Anlatım yönergesi

Somut, denetlenebilir davranışlar olarak yazıldı — "sinematik ol" ne modelin uyabileceği
ne de inceleyenin doğrulayabileceği bir talimattır:

> Read this as a deep, adult male storyteller narrating a documentary. Speak about 10-15%
> slower than a normal conversational pace, with a calm, controlled, cinematic delivery
> that still sounds natural and human. Put clear stress on the words that carry the
> meaning, especially proper names and the decisive turn in each sentence. Take deliberate
> short pauses at commas and full stops, and a slightly longer beat before the final
> sentence. Do not over-dramatise: avoid exaggerated movie-trailer delivery, avoid
> shouting, and do not rush the ending.

### Nerede saklanıyor

`decision.narration = {preset, voice, narration_direction, language_code}` — plan
**karar bloğunun** içinde, yani plan onay hash'inin parçası. Ön ayarı değiştirmek plan
hash'ini değiştirir → ses ve render onayları düşer. TTS sesi ve yönergeyi **plandan** alır;
çağıranın varsayılanı ezilir (test: `plan-voice-wins-over-caller`), çünkü onaylanandan
başka bir sesle seslendirmek sessiz bir içerik değişikliğidir.

`direct_clip_series` hâlâ hiç `narration` taşımaz — açıkça istense bile.

### Ses incelemesi ön ayar-farkında

Ön ayar denetlenebilir bir şey vaat ettiği için liste denetlenebilir olanı sorar
(anahtarlar **değişmez**, dolayısıyla yeniden etiketleme hiçbir işareti geçersiz kılmaz):

- **`pronunciation` → "Özel isim telaffuzu"**: *"Are the proper names pronounced correctly
  and consistently (Daemon, Rhaenyra, Targaryen)? Listen for each one separately, not just
  the first."*
- **`tone` → "Dramatik vurgu ve tempo"**: *"...clearly slower than normal speech, with real
  stress on the words that carry the meaning and deliberate short pauses — WITHOUT tipping
  into exaggerated movie-trailer delivery?"*

Voice Studio ayrıca ön ayar rozetini gösterir ve ses plandaki sesle uyuşmuyorsa
*"Ses plandaki anlatım ön ayarında DEĞİL"* uyarısı verir. Faz 0 seçicisi sesin
**belgelenmiş karakteristiğini ve seçim gerekçesini** açılır bir bölümde gösterir.

### Canlı doğrulama

| | |
|---|---|
| Gerçek API çağrısı | **1** `generate_content` (sayaçlı proxy ile ölçüldü) |
| Model | `gemini-3.1-flash-tts-preview` — fallback KAPALI, retry 0 |
| Ses / dil / ön ayar | **`Charon`** · `en-US` · `deep_storyteller` — üçü de plandan |
| Süre | **16.28 sn** · 24000 Hz · 1 kanal · 16 bit PCM · 781.484 bayt |
| Durum | **`review_required`** — onaylanmadı |

Metin: *"Daemon Targaryen knew that Rhaenyra's claim could reshape the fate of House
Targaryen. But one reckless decision could bring the entire dynasty to its knees."*

### Colab tek-dosya modu

`narration_presets`, gömülü modül listesine `google_ai`'dan **sonra**, `source_strategy`'den
**önce** eklendi (source_strategy onu import ediyor). Sırasız bir ekleme tek-dosya
notebook'ta Faz 0'ı import hatasıyla öldürürdü; `verify_local.py` bunu yakaladı.

### Kalan bloker

**Üretilen ses dinlenmedi.** Mekanik doğrulandı: ses kayıttan geliyor, ön ayar karar
hash'inde, TTS plandaki sesi ve yönergeyi kullanıyor, liste doğru soruları soruyor.
Tempo gerçekten %10-15 yavaş mı, vurgu doğru kelimelerde mi, "Rhaenyra" doğru okunuyor mu
ve fragman tonuna kaçmıyor mu — bunların hiçbiri doğrulanmış değil.

## 13q. `deep_storyteller` ince ayarı (yalnızca yönerge)

**Durum:** `verification_required` (ses üretildi, **insan dinlemesi bekliyor**)

`Charon` ve `en-US` **aynı**. Pipeline mantığı, arayüz, model yönlendirmesi ve ses kaydı
**değişmedi**. Değişen tek şey `deep_storyteller` ön ayarının anlatım yönergesi.

### Yeni yönerge

> Read this as a deep, adult male storyteller narrating a documentary. Speak about **5-8%**
> slower than a normal conversational pace: noticeably measured, but not laboured. Keep the
> delivery calm, controlled and cinematic while still sounding natural and human. **Let
> emphasis fall where it naturally would at sentence level; do not deliberately stress every
> important word or proper name.** Keep pauses at commas and full stops **short and
> unforced**, with only a **slight** beat before the final sentence. Do not rush, do not
> shout, and avoid exaggerated movie-trailer delivery.

| | Önce | Sonra |
|---|---|---|
| Tempo | ~%10-15 yavaş | **~%5-8 yavaş**, "ölçülü ama ağır değil" |
| Vurgu | "önemli kelimelerde, özellikle özel isimlerde belirgin vurgu" | **cümle düzeyinde doğal akış**; kelime kelime vurgulama yok |
| Duraklama | "bilinçli kısa duraklamalar" + son cümleden önce **daha uzun** bekleme | **kısa ve zorlamasız**, son cümleden önce yalnızca **hafif** bir bekleme |
| Ton / kaçınılacaklar | derin, sakin, kontrollü; acele/bağırma/fragman yok | **aynı** |

### Zorunlu yan değişiklik: inceleme sorusu

Ses inceleme listesindeki `tone` sorusu ön ayarın **vaadini** tarif eder. Eski hâli
*"anlamı taşıyan kelimelerde gerçek vurgu ve bilinçli kısa duraklamalar"* diye soruyordu —
ön ayarın artık açıkça **istemediği** şey. Bırakılsaydı liste, talep edilenin tersini
onaylatırdı. Soru ön ayarla hizalandı:

> ...slightly slower than normal speech and measured but not laboured, with emphasis falling
> **naturally at sentence level rather than pushed onto every important word** -- and WITHOUT
> tipping into exaggerated movie-trailer delivery?

Etiket "Dramatik vurgu ve tempo" → **"Vurgu ve tempo"**. Madde **anahtarı** değişmedi,
dolayısıyla mevcut hiçbir işaret geçersizleşmedi. `pronunciation` maddesi olduğu gibi
kaldı: doğru telaffuz istenmeye devam ediyor, değişen yalnızca özel isimlerin ayrıca
*vurgulanmaması*.

### Canlı doğrulama

| | |
|---|---|
| Gerçek API çağrısı | **1** `generate_content` — fallback KAPALI, retry 0 |
| Model | `gemini-3.1-flash-tts-preview` |
| Ses / dil / ön ayar | `Charon` · `en-US` · `deep_storyteller` (üçü de plandan) |
| Süre | **14.60 sn** (önceki yönergeyle 16.28 sn — %10 daha kısa, beklenen yönde) |
| Biçim | 24000 Hz · 1 kanal · 16 bit PCM · 700.844 bayt |
| Durum | **`review_required`** — onaylanmadı |

Aynı test metni: *"Daemon Targaryen knew that Rhaenyra's claim could reshape the fate of
House Targaryen. But one reckless decision could bring the entire dynasty to its knees."*

Süre farkı tempo değişiminin **etkisini gösterir, kalitesini değil** — 16.28 → 14.60 sn
istenen yönde ama "doğru mu" sorusunun cevabı değil.

### Kalan bloker

**Üretilen ses dinlenmedi.** Tempo gerçekten %5-8 aralığında mı, vurgu artık zorlama
olmadan doğal mı, duraklamalar fazla kısalmış mı ve "Rhaenyra" hâlâ doğru okunuyor mu —
hiçbiri doğrulanmış değil. İki ses dosyası yan yana duruyor, karşılaştırmalı dinleme
mümkün.

## 14. Test ve doğrulama panosu

| Kontrol | Son durum | Kanıt |
|---|---|---|
| Notebook JSON/Python sözdizimi | Geçti | `verify_local.py` |
| Gömülü modül hash senkronu | Geçti | `tools/refresh_embedded_payloads.py --check` |
| Yerel analiz smoke testi | Geçti | `LOCAL_SMOKE_TEST_OK` |
| Yerel render smoke testi | Geçti | `RENDER_SMOKE_TEST_OK` |
| Approval gate regresyonları | Geçti | `APPROVAL_GATE_REGRESSION_OK`, `RENDER_APPROVAL_HARDBLOCK_OK` |
| YouTube keşif mantığı | Mock test geçti | `YOUTUBE_DATA_MOCKED_TESTS_OK` |
| Google model/TTS routing | Mock test geçti | `GOOGLE_AI_ROUTER_MOCKED_TESTS_OK` |
| Faz 0 strateji kapısı ve plan onayı | Deterministik test geçti | `PHASE0_SOURCE_STRATEGY_TESTS_OK` |
| Faz 0 master notebook entegrasyonu | Gerçek ffmpeg + mock sağlayıcı geçti | `MASTER_NOTEBOOK_ALL_PHASES_OK` |
| Creator Console (backend+frontend sözleşmesi) | Gerçek yerel + mock sağlayıcı geçti | `CREATOR_CONSOLE_VERIFICATION_OK` (`python verify_console.py`) |
| Adversarial fault-injection (10 aile, 70 senaryo) | Geçti | `FAULT_INJECTION_SUITE_OK` (`python verify_faults.py`) |
| Gerçek tarayıcı UI testleri (Chromium/Playwright) | Geçti | `BROWSER_SUITE_OK` (`python verify_browser.py`), 85 kontrol |
| Gerçek kaynak uçtan uca önizleme (izole) | Geçti | `REAL_PREVIEW_OK` (`python verify_real_preview.py`), ffprobe doğrulamalı |
| Gerçek YouTube bağlantısı | Manuel temel test geçti | Kullanıcının Colab `videos.list` testi |
| Gerçek Gemini script | **Geçti** | `LIVE_SCRIPT_OK` (`python verify_live_script.py --i-authorize-one-live-call`), 1 gerçek çağrı, `gemini-3.6-flash` |
| Gerçek Gemini TTS | **Geçti** | `LIVE_TTS_OK` (`CREATOR_CONSOLE_LIVE_TTS=1 python verify_live_tts.py --i-authorize-one-live-call`), 1 gerçek çağrı, `gemini-3.1-flash-tts-preview`, 8.44 sn |
| Gerçek uçtan uca final Short | Bekliyor | M0.5 canlı Colab koşusu |

## 15. Bir sonraki çalışma oturumu

1. Güncel master notebook'u Colab'a yükle.
2. `GeminiKey` ve `YoutubeKEY` notebook erişimlerini kontrol et.
3. Araştırma toggle'larını kapalı tut.
4. Gerçek kaynak videoyu Drive'a yükle ve `SOURCE_VIDEO_PATH` değerini ayarla.
5. Yalnızca video analizini çalıştır.
6. Moment sonuçlarını indirip/inceleyip seçim yap.
7. Script fazını gerçek Gemini ile çalıştır ve onayla.
8. Gemini TTS fazını çalıştır, sesi dinle ve onayla.
9. Preview render üret ve izle.
10. Kalite uygunsa final render üret.
11. Bu dosyadaki M0.1–M0.6 checkbox'larını kanıta göre güncelle.

## 16. İlerleme günlüğü

| Tarih | Değişiklik | Etkilenen milestone | Kanıt/sonuç |
|---|---|---|---|
| 2026-08-03 | Ana pipeline ile YouTube araştırma yan kolu ayrıldı. | M0.0, R0.1 | `feeds_main_pipeline=false` sözleşmesi ve ayrı notebook özeti |
| 2026-08-03 | Gemini TTS ana seslendirme rotası yapıldı. | M0.4, M0.5 | `RUN_GOOGLE_TTS=True`, `TTS_PROVIDER='gemini'` |
| 2026-08-03 | Başlangıç roadmap'i yeniden projeye aktarıldı. | Tümü | `ROADMAP.md`, bu dosya |
| 2026-08-03 | Colab Moment Explorer preview ve hash-bağlı seçim kapısı eklendi. | M0.2, M0.3 | `moment_selection.json`, preview/manifest SHA-256 doğrulaması |
| 2026-08-03 | Her revizyondan sonra milestone kaydı zorunlu hale getirildi ve sıradaki gerçek doğrulama M0.1 + M0.2 olarak belirlendi. | Tümü | `Zorunlu revizyon kayıt protokolü` eklendi. |
| 2026-08-03 | Colab ve Drive için kalıcı proje kökü kullanıcıdaki gerçek klasöre göre güncellendi. | M0.1–M0.6, R0.1–R0.3 | `/content/drive/MyDrive/AVRUPA YAKASI/YoutubeOtomationv0`; ana ve araştırma notebook yolları eşitlendi. |
| 2026-08-03 | Yüklenen kaynak videonun tam Drive yolu ana notebook'a kaydedildi; güvenli başlangıç sözleşmesi gereği faz toggle'ları varsayılan kapalı bırakıldı. | M0.1 | `SOURCE_VIDEO_PATH` gerçek MP4 yolunu gösteriyor; Colab koşusunda `RUN_VIDEO_ANALYSIS=True` kullanıcı tarafından açılacak. Canlı sonuç bekleniyor. |
| 2026-08-03 | Kaynak video girişi sabit Drive klasörüne bağlandı; dosya adı girme zorunluluğu kaldırıldı. | M0.1 | `source_videos/` içindeki en yeni desteklenen video otomatik seçiliyor; açık override ve deterministik seçim testi eklendi. |
| 2026-08-03 | Faz 1 gerçek kaynak videoyla Colab'da tamamlandı ve dört hot-moment preview üretildi. | M0.1, M0.2, M0.5 | `execution_status=completed`, `artifact_status=review_required`, `4 aday -> hot_moments.json`; klip kalite incelemesi bekleniyor. |
| 2026-08-03 | Ana ürün akışı orijinal ses + Whisper zaman kodlu altyazı olarak düzeltildi; Gemini script/TTS isteğe bağlı anlatımlı moda taşındı. | M0.3–M0.6 | `PIPELINE_MODE='original_audio_subtitles'`; canonical orijinal-ses render yolu, moda duyarlı onay kapısı ve gerçek FFmpeg/Whisper regresyonları eklendi. |
| 2026-08-03 | Caption Engine v1 eklendi: konuşulan kelimeyi sarı renkle takip eden dinamik ASS altyazı ana render yoluna bağlandı. | M0.5, M0.6 | Word timestamp gruplama, `subtitles_dynamic.ass`, ayarlanabilir font/renk/konum, düz SRT fallback ve gerçek FFmpeg render testi. |
| 2026-08-03 | Caption tema yönlendiricisi eklendi; Game of Thrones evreni için sabit Cinzel profili ve doğrulanmış font varlığı zorunlu yapıldı. | M0.6 | GOT/HOTD/Targaryen/Westeros anahtarları `game_of_thrones` profiline gider; resmi Google Fonts dosyası + OFL + sabit SHA-256, manifest tema/font kaydı ve sessiz font fallback yasağı. |
| 2026-08-03 | Sıradaki canlı doğrulama Caption Engine preview kalite kontrolüne ilerletildi. | M0.6 | Gerçek Colab renderında Cinzel, sarı aktif kelime senkronu, satır kırılımı, güvenli alan, crop ve orijinal ses kontrol edilecek. |
| 2026-08-03 | Gerçek Colab preview görsel kalite incelemesi yapıldı. | M0.5, M0.6 | Font, boyut, dört kelimelik kart, sarı aktif-kelime eşleşmesi, güvenli alan, crop ve kesintisiz orijinal ses kabul edildi; nadir eski satır birikmesi ve klip başı/sonu bağlam kaybı tespit edildi. |
| 2026-08-03 | Caption olayları sıfır çakışmalı yapıldı; hot-moment kesimi konuşma dolgusu ve yalnızca dışa yönlü sahne sınırlarıyla genişletildi. | M0.2, M0.6 | `ASS_ZERO_OVERLAP_OK`, `DIRECTIONAL_MOMENT_BOUNDARIES_OK` ve tam `verify_local.py` çıkış kodu 0; güncel Colab preview ile canlı tekrar doğrulama bekleniyor. |
| 2026-08-03 | Son tüketici erişimi için siyah–turuncu Creator Console pre-alpha ayrıntılı milestone olarak planlandı. | M0.9, M1.0, M1.1 | Ekranlar, tasarım sistemi, pipeline/UI sınırı, kabul kriterleri ve gelecekteki çok kullanıcılı sahiplik–izolasyon altyapısı tanımlandı; uygulama henüz başlamadı. |
| 2026-08-03 | ZORUNLU Faz 0 kaynak işleme stratejisi eklendi: iki mod, tavsiye-only öneri, hash-bağlı plan onayı ve alt faz kapıları. | M0.05, M0.1–M0.6 | `local_pipeline/source_strategy.py`, `approval.py` (`processing_plan` türü), master notebook Faz 0/0a/0b/0c, `PHASE0_SOURCE_STRATEGY_TESTS_OK` (9 kontrol) + `MASTER_NOTEBOOK_ALL_PHASES_OK` (plan yokken faz durur, direct_clip_series'te SIFIR API çağrısı, narrated_theory için ayrı plan+onay); `verify_local.py` çıkış kodu 0. |
| 2026-08-03 | M0.9 Creator Console pre-alpha uygulandı: stdlib HTTP backend, build'siz siyah–turuncu frontend, kalıcı iş modeli, sahiplik/storage izolasyonu, audit olayları, sunucu-tarafı secret referansları. | M0.9 | `creator_console/` (11 modül + 3 statik dosya), `verify_console.py` (15 bölüm) → `CREATOR_CONSOLE_VERIFICATION_OK`, çıkış kodu 0; `python -m creator_console` gerçek soket üzerinde doğrulandı; restart kurtarma kusuru bulundu ve düzeltildi (`recover_abandoned(ignore_lease=True)`). |
| 2026-08-03 | Adversarial fault-injection ve kurtarma turu: 7 senaryo ailesi, 46 senaryo. 4 gerçek kusur bulundu ve düzeltildi (secret redaksiyon boşluğu, upstream onay zinciri, bağlamayan plan onayının faz kapılarını açması, Windows atomik yazım çekişmesi). | M0.05, M0.9, M0.3–M0.6 | `creator_console/redaction.py` (yeni), `pipeline_service.approval_chain()`, `source_strategy.plan_allows_phase()` bağlama kontrolü, `storage.atomic_write_bytes()` yeniden deneme; `verify_faults.py` → `FAULT_INJECTION_SUITE_OK` (46/46); `verify_local.py` ve `verify_console.py` çıkış kodu 0. |
| 2026-08-03 | Creator Console için proje köküne çift tıklanabilir Windows başlatıcısı eklendi. | M0.9 | `Start Creator Console.exe`, derlenebilir `tools/StartCreatorConsole.cs`; sanal ortam/proje kontrolü, ayrı sunucu terminali, hazır olma bekleme ve tarayıcıyı otomatik açma; `--check` exit code 0. |
| 2026-08-03 | Creator Console için güvenli, çift tıklanabilir Windows kapatıcısı eklendi. | M0.9 | `Stop Creator Console.exe`, `tools/StopCreatorConsole.cs` ve token bağlı yerel graceful-shutdown kanalı; izole gerçek süreç testinde Stop exit 0, sunucu kapandı ve kontrol dosyası temizlendi; başka Python süreçleri hedeflenmez. |
| 2026-08-03 | Creator Console'un boş görünmesine neden olan frontend sözdizimi kusuru giderildi. | M0.9 | `screenSettings()` kapanış yapısı sadeleştirildi; gerçek headless Chrome sonucu: HTTP 200, JS compile OK, 0 page error, 9 navigasyon düğmesi, Projeler başlığı ve Proje oluştur aksiyonu görünür. |
| 2026-08-03 | Creator Console için Git-dışı yerel API anahtarı kasası eklendi. | M0.9 | `.env.local`, izinli-değişkenli `creator_console/env_file.py`, otomatik sunucu tarafı yükleme ve Ayarlar ekranında yalnızca configured/missing görünümü; ortam değişkeni önceliği, bilinmeyen anahtar reddi ve secret-değersiz özet testi `LOCAL_ENV_SECRET_FILE_OK`. |
| 2026-08-03 | Kalıcı “Kayıtlar” sekmesi ve düzenlenebilir proje arşivi eklendi. | M0.9 | Proje adları tıklanabilir; ad/not PATCH API'si, `project.updated` audit olayı, yeniden başlatma kalıcılığı ve kaynak/artefakt koruması. Gerçek HTTP + headless Chrome testi `CONSOLE_ARCHIVE_PERSISTENCE_E2E_OK`; test sırasında eksik `do_PATCH` kusuru bulunup düzeltildi. |
| 2026-08-03 | Faz 0 işleme planı Creator Console üzerinden ayrıntılı ve kalıcı biçimde düzenlenebilir yapıldı. | M0.05, M0.9 | `PATCH /api/projects/{id}/plan`, kanonik yeniden-planlama, `plan.updated` + `approval.invalidated` audit olayları; mod, çıktı, süre, moment, teori, kadraj, caption ve not alanları. Gerçek HTTP + headless Chrome testi `PHASE0_PLAN_EDITOR_BROWSER_E2E_OK`; tam konsol akışındaki yeni edit/onay regresyonu geçti. |
| 2026-08-03 | Creator Console'a sekme bazlı manuel yeniden çalıştırma ve kapı-farkındalıklı otomatik akış eklendi. | M0.05, M0.9, M0.1–M0.6 | Her manuel tıklama yeni `run_request_id` ile gerçek rerun üretir, aynı istek tekrarı deduplikasyonda kalır; Moment Explorer analiz düğmesi kalıcıdır. `POST /api/projects/{id}/auto-run` sıradaki fazı seçer, insan kararlarını atlamaz. Windows mutlak preview yolunun tarayıcıda bozulması da sahiplik-korumalı göreli medya rotasıyla düzeltildi. Gerçek HTTP + Chrome + FFmpeg/Whisper testi `CONSOLE_AUTO_FLOW_MOMENT_E2E_OK`, rota testi `MOMENT_PREVIEW_RELATIVE_ROUTE_OK`; mevcut HOTD projesinde 1 kart ve HTTP 200 preview `EXISTING_PROJECT_MOMENT_VISIBLE_OK` ile doğrulandı. |
| 2026-08-03 | Console sertleştirme turu: gerçek Playwright tarayıcı testleri, ayrı süreçli worker + kontrollü öldürme, lease heartbeat, depolama arıza enjeksiyonu, kapalı hata kataloğu. 5 gerçek kusur bulundu ve düzeltildi. | M0.9 | `verify_browser.py` (42/42), `verify_faults.py` (70/70), `creator_console/{errors,faults,worker}.py` (yeni); `verify_local.py`, `verify_console.py`, `verify_faults.py`, `verify_browser.py` çıkış kodu 0. |
| 2026-08-03 | Otomatik akış sunucuda kalıcı durum makinesine çevrildi; her sekmeye düzenlenebilir manuel kontroller ve faz durum paneli eklendi; görünüm sınırında host-yolu temizleyicisi; gerçek HOTD kaynağıyla izole uçtan uca önizleme üretildi. | M0.9, M0.05, M0.6 | `creator_console/automation.py` (yeni), `verify_real_preview.py` (yeni), `app.py` (`scrub_host_paths`, automation_action), `api.py` (`/automation`), `static/app.js`; `verify_local.py`/`verify_console.py`/`verify_faults.py`/`verify_browser.py`/`verify_real_preview.py` çıkış kodu 0; önizleme 540×960 h264+aac 42.77s, `review_required`. |
| 2026-08-03 | Yerel ortam preflight kontrolü (24 kontrol, çözüm metinleriyle) ve tıklanabilir doğrusal pipeline ilerleme göstergesi eklendi; Ayarlar'a preflight paneli, her ekrana engel banner'ı. | M0.9 | `creator_console/status.py` (yeni), `app.py`/`api.py` (`/api/preflight`, `progress`), `static/app.{js,css}`; `verify_console.py` bölüm 19 ve `verify_browser.py` bölüm 13 eklendi → 71/71; `verify_local.py`, `verify_faults.py` (70/70), `verify_real_preview.py` çıkış kodu 0. |
| 2026-08-03 | Render Studio inceleme deneyimi: büyük oynatıcı, 12 alanlık teknik özet, 6 maddelik hash-bağlı insan kontrol listesi (final onayı kapatır), notlu 'revizyon gerekli' ve hedef sekmeye yönlendirme. | M0.9, M0.6 | `creator_console/render_review.py` (yeni), `pipeline_service.approve('render')` ek kapı, `/api/projects/<id>/render-review`, `static/app.{js,css}`; `verify_console.py` bölüm 20 + `verify_browser.py` bölüm 15 → 85/85; `verify_local.py`, `verify_faults.py` (70/70), `verify_real_preview.py` çıkış kodu 0. |
| 2026-08-03 | narrated_theory yolu TEK gerçek Gemini çağrısıyla doğrulandı (`gemini-3.6-flash`, 587 karakter Türkçe script, `review_required`); tek-çağrı garantisi için `max_transient_retries` script zincirine uçtan uca bağlandı. | M0.3, M0.9 | `verify_live_script.py` (yeni), `local_pipeline/script_generation.py` + `creator_console/pipeline_service.py` (retry parametresi); gömülü payload'lar yenilendi; `verify_local.py`, `verify_console.py`, `verify_faults.py` (70/70), `verify_browser.py` (85/85), payload `--check` çıkış kodu 0. |
| 2026-08-03 | 13 yetenek için fonksiyonel teşhis sistemi (kayıt + `GET/POST /api/diagnostics` + Ayarlar paneli); her kontrol izole geçici fikstürde gerçekten çalıştırılıyor. Bu tur 5 gerçek sorun bulundu ve düzeltildi (fikstür kendi kaynağını bozuyordu, canlı-app sızıntısı, gezinme testi yanlış nedenle geçiyordu, blocker'da host yolu, kontrol sırası). | M0.9 | `creator_console/diagnostics.py` (yeni), `verify_diagnostics.py` (yeni, 109 kontrol, 10 hata enjeksiyonu), `creator_console/api.py`, `creator_console/app.py`, `static/app.js`, `static/app.css`, `verify_browser.py` (+22 → 107/107); `verify_local.py`, `verify_console.py`, `verify_faults.py` (70/70), payload `--check` çıkış kodu 0. |
| 2026-08-03 | Etkileşimli pipeline akış haritası: 9 adım, 6 durum, sunucu türetimli, mod-farkında atlama, engel/çözüm mesajı, tıklanabilir navigasyon; eski ilerleme göstergesi kaldırıldı (kopya yok). Mod seçilmeden Script/Ses'i `skipped` sayan gerçek hata düzeltildi. | M0.9 | `creator_console/status.py` (`build_workflow_map`), `creator_console/app.py` (`progress` → `workflow`), `static/app.js` (`workflowMap`), `static/app.css` (`.wf*`), `verify_console.py` (+28), `verify_browser.py` (107 → 142), `verify_live_script.py`; `verify_local.py`, `verify_faults.py` (70/70), `verify_diagnostics.py` (109/109), payload `--check` çıkış kodu 0. |
| 2026-08-04 | Paylaşılan zorunlu-alan doğrulaması (turuncu *, `aria-required`, efsane, satır içi hata, ilk geçersiz alana odak) ve 9 aktivite için sunucuda kalıcı tqdm tarzı ilerleme panelleri. Üç gerçek hata düzeltildi: plan imzası boş bırakılınca `"console"` diye sahteleniyordu, `Number(...) || null` yazılan değeri sessizce siliyordu, revizyon düğmesi `ReferenceError` atıyordu. | M0.9 | `creator_console/progress.py` (yeni), `verify_forms_progress.py` (yeni, 80 kontrol), `creator_console/{api,app,errors,automation,diagnostics,pipeline_service}.py`, `local_pipeline/{video_analysis,render_utils}.py` (opsiyonel `progress` gözlemcisi), `static/app.js`, `static/app.css`, `verify_browser.py` (142 → 186), `verify_console.py`, `verify_diagnostics.py` (110/110); `verify_local.py`, `verify_faults.py` (70/70), `verify_real_preview.py`, payload `--check` çıkış kodu 0. |
| 2026-08-04 | Script Studio artık onaylanacak metni gösteriyor (okunamayan bir metnin hash'ini onaylamak inceleme değildir); `hook`/`estimated_duration_sec` her sağlayıcı yolunda kaybolmaktan kurtarıldı; FFmpeg son kodlaması `-progress` ile gerçekten ölçülüyor ve ayrı bir iç çubukta gösteriliyor. | M0.9 | `local_pipeline/{script_generation,render_utils}.py`, `creator_console/{app,fixtures}.py`, `static/app.js`, `static/app.css`, `verify_forms_progress.py` (80 → 105), `verify_browser.py` (186 → 198); `verify_local.py`, `verify_console.py`, `verify_faults.py` (70/70), `verify_diagnostics.py` (110/110), `verify_real_preview.py`, payload `--check` çıkış kodu 0. |
| 2026-08-04 | Yerel entegrasyon + UX kabul geçişi: gerçek HOTD kaynağı ve gerçek tarayıcı ile tam akış (109/109). Bir gerçek hata bulundu ve düzeltildi: inceleyen kişi adı her yeniden çizimde siliniyor, 6 maddeyi işaretlemek için 6 kez yazmak gerekiyordu — `verify_browser.py` bunu her tıklamadan önce adı yeniden doldurarak gizliyormuş; geçici çözüm kaldırıldı. | M0.9 | `verify_acceptance.py` (yeni, 109 kontrol), `static/app.js` (`state.renderReviewer`, iş tablosu retry test kancası), `verify_browser.py` (198 → 199); `verify_local.py`, `verify_console.py`, `verify_faults.py` (70/70), `verify_diagnostics.py` (110/110), `verify_forms_progress.py` (105/105), `verify_real_preview.py`, payload `--check` çıkış kodu 0. |
| 2026-08-04 | Canlı Gemini TTS yolu TEK gerçek çağrıyla doğrulandı (`gemini-3.1-flash-tts-preview`, Kore/tr-TR, 8.44 sn, 24kHz mono 16-bit, `review_required`); `CREATOR_CONSOLE_LIVE_TTS=1` + açık bayrak çift koruması, tek-çağrı garantisi için router parametreleri `run_tts`'e bağlandı. | M0.3, M0.9 | `verify_live_tts.py` (yeni), `creator_console/pipeline_service.py` (`run_tts` router parametreleri); `verify_local.py`, `verify_console.py`, `verify_faults.py` (70/70), `verify_diagnostics.py` (110/110), `verify_forms_progress.py` (105/105), `verify_browser.py` (199/199), `verify_acceptance.py` (109/109), payload `--check` çıkış kodu 0. |
| 2026-08-04 | Voice Studio inceleme deneyimi: güvenli gömülü oynatıcı, teknik künye (ses/model/dil/süre/biçim/örnekleme/kanal/script hash), seslendirilen script metni, 6 maddelik dinleme listesi, hash bağlı bayatlama, revizyon ve TTS yeniden üretme. Kurallar `media_review.py` içinde ortaklaştırıldı (kopya yok). İki gerçek hata düzeltildi: 'Revizyonu kapat' render'da hiçbir şey yapmıyordu (verdict kalıcı `needs_revision`), ve ses onay paneli liste eksikken açıktı. | M0.9 | `creator_console/media_review.py` + `voice_review.py` (yeni), `render_review.py` (ortak çekirdeğe devretti), `pipeline_service.py`, `api.py`, `app.py`, `static/app.js`, `static/app.css`, `verify_voice_review.py` (yeni, 85 kontrol), `verify_browser.py` (199 → 228); `verify_local.py`, `verify_console.py`, `verify_faults.py` (70/70), `verify_diagnostics.py` (110/110), `verify_forms_progress.py` (105/105), `verify_acceptance.py` (109/109), `verify_real_preview.py`, payload `--check` çıkış kodu 0. |
| 2026-08-04 | İçerik dili uçtan uca narrated_theory ayarı oldu: plan kararında zorunlu (`en-US` varsayılan), onay hash'inin parçası, script prompt'unda ilk kural, TTS'e plandan geçiyor, ses incelemesi 'English naturalness' diye yeniden etiketleniyor. Dil değişimi script/ses/render onaylarını düşürüyor ve hiçbir şeyi çevirmiyor. `direct_clip_series` değişmedi. 1 canlı İngilizce TTS çağrısı (9.00 sn, `review_required`). | M0.3, M0.9 | `local_pipeline/{source_strategy,script_generation}.py`, `creator_console/{pipeline_service,api,voice_review}.py`, `static/app.js`, `verify_content_language.py` (yeni, 47 kontrol), `verify_live_tts.py` (İngilizce metin), `verify_browser.py` (228 → 234); `verify_local.py`, `verify_console.py`, `verify_faults.py` (70/70), `verify_diagnostics.py` (110/110), `verify_forms_progress.py` (105/105), `verify_voice_review.py` (85/85), `verify_acceptance.py` (109/109), `verify_real_preview.py`, payload `--check` çıkış kodu 0. |
| 2026-08-04 | `deep_storyteller` anlatım ön ayarı: ses **Charon** (belgelenen karakteristik *Informative*; Fenrir/Algenib/Gacrux/Kore gerekçeli elendi, kayıttan seçildi, uydurulmadı), ~%10-15 yavaş + vurgu + bilinçli duraklama + fragman tonu yok yönergesi. Plan karar bloğunda saklanıyor, değişimi ses/render onaylarını düşürüyor; ses incelemesi özel isim telaffuzu ve dramatik vurguyu soruyor. 1 canlı TTS çağrısı (16.28 sn, `review_required`). | M0.3, M0.9 | `local_pipeline/narration_presets.py` (yeni), `local_pipeline/source_strategy.py`, `creator_console/{pipeline_service,api,app,voice_review}.py`, `static/app.js`, `static/app.css`, `verify_content_language.py` (47 → 86), `verify_live_tts.py`, `verify_local.py` (+giriş noktaları), `experiments/colab/00_*.ipynb` (modül sırası); `verify_console.py`, `verify_faults.py` (70/70), `verify_diagnostics.py` (110/110), `verify_forms_progress.py` (105/105), `verify_voice_review.py` (85/85), `verify_browser.py` (234/234), `verify_acceptance.py` (109/109), payload `--check` çıkış kodu 0. |
| 2026-08-04 | `deep_storyteller` yönergesi ince ayarlandı: tempo ~%10-15 → **~%5-8**, vurgu kelime kelime → **cümle düzeyinde doğal**, duraklamalar kısaltıldı. Charon/en-US ve pipeline/arayüz/yönlendirme/ses kaydı değişmedi. Ses inceleme `tone` sorusu ön ayarın tersini doğrulamaması için hizalandı (anahtar aynı, işaretler korundu). 1 canlı TTS çağrısı: 14.60 sn (öncekiyle 16.28 sn), `review_required`. | M0.3, M0.9 | `local_pipeline/narration_presets.py`, `creator_console/voice_review.py`, `verify_content_language.py` (86 → 93); `verify_local.py`, `verify_console.py`, `verify_voice_review.py` (85/85), `verify_faults.py` (70/70), `verify_diagnostics.py` (110/110), `verify_forms_progress.py` (105/105), `verify_browser.py` (234/234), `verify_acceptance.py` (109/109), `verify_real_preview.py`, payload `--check` çıkış kodu 0. |
| 2026-08-05 | Moment Explorer aday sayısı ve arayüzü düzeltildi: Console hedef sayıya göre gerçek klip süresine yayılan pencereler üretiyor; eski 20 sn/5 aday durumu artık 1 yerine 5 aday üretebiliyor. Sonuç galerisi, ayarlar ve tek-seçim alanı ayrıldı; yinelenen faz paneli kaldırıldı, otomasyon kontrolleri akış haritasına birleştirildi. | M0.2, M0.9 | Gerçek FFmpeg/Whisper + Chromium: ilk koşuda 5 kart, 8 sn/2 hedef yeniden koşusunda `2 / 2` ve 2 kart, preview HTTP 200, yenileme kalıcılığı ve mobil 0 px taşma; `verify_console.py` exit 0, `verify_browser.py` 236/236 exit 0, `verify_local.py` exit 0, `ADAPTIVE_CANDIDATE_COUNT_OK`. |
| 2026-08-05 | Altyazı doğruluk ve font turu: Console'un eski `tiny` üretim yolu kaldırıldı; hızlı/uyumluluk tabanı `small`, yeni planların önerilen ve varsayılan altyazı modeli `large-v3-turbo` oldu. Faz 1'de saptanan dil renderda yeniden kullanılıyor; açık dil seçimi ve GOT/HOTD özel ad bağlamı (Mysaria dahil) plan hash'ine bağlanıyor. Otomatik GOT fontu, lisanslı Trajan yerine açık lisanslı ve küçük ekranda daha okunabilir `Marcellus SC` oldu; Cinzel karşılaştırma/geri dönüş seçeneği olarak korundu. | M0.2, M0.6, M0.9 | Resmî Google Fonts Marcellus SC TTF + OFL, sabit SHA-256; gerçek HOTD üzerinde `small` ile izole render teknik kontrolden geçti fakat insan gözüyle bariz kalan ASR hataları daha kaliteli yeni-plan varsayılanını gerekli kıldı. Gerçek FFmpeg/libass `ORIGINAL_AUDIO_SUBTITLE_RENDER_OK`, `verify_local.py` ve `verify_console.py` exit 0; güncel Chromium koşusunda ilgili yeni plan, 5 aday ve oynatma kontrolleri geçti (tam uzun paket ortam süre sınırına takıldı). Hiçbir canlı API/yükleme/yayın yok. |
| 2026-08-05 | Proje yaşam döngüsü ve geriye dönük revizyon eklendi: her projede değerlendirme/plan/analiz/moment/script/ses/render aşamasına geri dönme, Script Studio'da insan düzenlemesi, kaynak korunarak veya tamamen sıfırlama ve proje silme. Eski çıktılar `.revisions` arşivine, silinen proje workspace `.trash` alanına taşınır; aktif iş varken mutasyon reddedilir, sıfırlama/silme proje adını birebir doğrulatır. Render revizyonu artık doğrudan Script veya Voice Studio'yu hedefleyebilir. | M0.9 | `creator_console/{pipeline_service,app,api,storage,server,audit,render_review,errors}.py`, `static/app.{js,css}`, `verify_project_lifecycle.py` 23/23 ve gerçek Chromium `verify_lifecycle_browser.py` 9/9; `verify_console.py` exit 0 (`CREATOR_CONSOLE_VERIFICATION_OK`). Hiçbir sağlayıcı çağrısı, yükleme veya yayın yapılmadı. |
| 2026-08-05 | Ayarlar ekranına GitHub token girişi eklendi. Yalnız `GITHUB_TOKEN` allowlist anahtarına, yerel `.env.local` dosyasına atomik yazılır; kayıt sonrası parola alanı temizlenir, değer API/DOM/log/artefakt yanıtlarına dönmez ve çalışma sürecinde hemen `configured` olur. Otomatik `.console_data` senkronu bilinçli olarak ayrı ve kapalı tutuldu. | M0.9 | `creator_console/{env_file,secrets_ref,app,api,errors,__main__}.py`, `static/app.{js,css}`, `.env.example`, `verify_secret_entry.py`: 15/15 gerçek API + Chromium; `verify_console.py` ve `verify_local.py` exit 0, secret scan geçti. |

| 2026-08-06 | Çalışan modüllere sahiplik kapsamlı, kalıcı ve kooperatif **Durdur** akışı eklendi. Kuyruktaki iş anında `cancelled`; çalışan iş ise `cancel_requested` olarak kalır, FFmpeg/Whisper güvenli kontrol noktasında gerçek alt süreci sonlandırdıktan sonra `cancelled` olur. Kullanıcıya işlem bitmeden sahte “durdu” durumu gösterilmez; iptal edilen iş yeniden denenebilir. Moment Explorer uzun video ayarları saniye yerine dakika tabanlı hale getirildi ve `video başı / orta / son / özel dakika` odak seçenekleri eklendi. Aralık gerçek video süresine göre sunucuda çözülür ve EOF sınırına kırpılır. | M0.2, M0.9 | `verify_cancel_long_video.py` 22/22; 2 saatlik kaynakta 10 dk başlangıç `00:00–10:00`, orta `55:00–65:00`, son `110:00–120:00`; gerçek uyuyan alt süreç 5 sn altında sonlandırıldı. `verify_console.py` exit 0, `verify_forms_progress.py` 105/105, `verify_browser.py` 241/241, `verify_faults.py` 70/70, `verify_local.py` exit 0. `local_pipeline` gömülü notebook payload'ları yenilendi ve senkron doğrulandı. |

Yeni bir çalışma tamamlandığında bu tabloya tarih, yapılan değişiklik ve somut
kanıt eklenmelidir. Checkbox yalnızca ilgili kanıt üretildiğinde işaretlenmelidir.
