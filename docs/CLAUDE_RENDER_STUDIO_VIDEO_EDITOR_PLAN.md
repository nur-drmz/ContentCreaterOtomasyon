# Claude Uygulama Görevi — Render Studio Hızlı Revize ve Video Editör

Bu belge Claude'a doğrudan verilecek uygulama promptudur. Amaç, yeniden keşif ve
gereksiz bağlam tüketimini azaltırken mevcut insan onayı, hash ve revizyon
güvenliğini korumaktır.

## Claude'a verilecek prompt

Bu repository üzerinde Render Studio Hızlı Revize panelini ve mimariye bağlı tek
klipli Video Editör akışını uygula. Yalnızca plan yazıp bırakma; aşağıdaki sırayı
izleyerek kodu, testleri ve kısa dokümantasyonu tamamla.

### Çalışma kuralları

1. Önce kökteki `AGENTS.md` talimatlarını oku ve codebase-memory MCP grafını
   kullan. Kod keşfinde önce `search_graph`, `trace_path`, `get_code_snippet`;
   aday yollar belirlendiğinde bir kez `check_index_coverage` kullan. Kapsama
   uyarısı bulunan aralıkları doğrudan kaynaktan doğrula.
2. Worktree kirli olabilir. Mevcut kullanıcı/başka ajan değişikliklerini geri
   alma, yeniden biçimlendirme bahanesiyle geniş dosya değişiklikleri yapma.
3. Tüm ayarlar output-slot bazlı olmalı. Slot 1 için çalışan çözüm diğer slotların
   artefaktlarını veya videolarını ezmemeli.
4. `hot_moments.json` ve `moment_selection` analiz/seçim kanıtıdır; video editörü
   bunları yerinde değiştirmemeli.
5. Eski renderlar silinmemeli. Etkilenen çıktılar mevcut revizyon/arşiv sistemiyle
   taşınmalı ve geri alınabilir kalmalı.
6. Ayar değişikliği otomatik publish yapmamalı. `publish_intent` ve geçerli final
   render onayı gerektiğinde düşürülmeli.
7. İstemciden keyfi FFmpeg argümanı, codec metni veya font dosya yolu kabul etme.
   Sunucu tarafında allowlist/preset kullan.
8. Script/TTS üretimini yalnızca gerçekten etkilendiğinde geçersiz kıl. Görsel
   revizyonlar gereksiz model tokeni veya TTS maliyeti üretmemeli.
9. Önce hedefli testleri, sonra ilgili geniş regresyon paketlerini çalıştır.
   Başarısız testi gizleme veya kabul kriterini gevşetme.
10. Commit/push yapma. Bitince değişen dosyaları, test sonuçlarını ve kalan gerçek
    medya doğrulamalarını kısa biçimde raporla.

### Doğrulanmış mevcut durum

- `creator_console/static/app.js::screenRender` Render Studio ayarlarını Faz 0
  kararından okuyor ve teknik özeti salt okunur gösteriyor.
- `creator_console/pipeline_service.py::render_adjustment` yalnızca
  `frame_zoom`, `frame_focus_x`, `frame_focus_y` saklıyor.
- `creator_console/pipeline_service.py::_run_render` canlı render ayarlarını
  onaylanan Faz 0 ayarlarıyla karşılaştırıyor; keyfi override şu anda drift
  hatasına dönüşüyor.
- `local_pipeline/render_utils.py::resolve_render_source` kaynak aralığını seçilen
  momentin `source_start_sec/source_end_sec` değerlerinden alıyor; ana render EDL
  clip sınırlarını henüz tüketmiyor.
- `creator_console/pipeline_service.py::update_edl_clip` ile EDL update ve preview
  uçları mevcut.
- `local_pipeline/scene_editor.py::trim_clip` yalnızca mevcut clip aralığının
  içine daraltıyor; clip'i kaynak içinde uzatma veya taşıma yapamıyor.
- `apps/web/src/screens/SceneEditorScreen.jsx` sabit 5–30 saniye değerleriyle bir
  prototip. Ürün akışı kabul edilmemeli; yalnızca mevcut API yeteneğinin kanıtı.
- Anlatımlı altyazı ve script editörleri zaten mevcut. Bunları bozma veya ikinci
  bir rakip editör oluşturma.

### Hedef veri akışı

```text
processing_plan (mod, dil, output sayısı, kaynak niyeti)
  -> moment_selection (değiştirilmeyen analiz kanıtı)
  -> EDL/edit decision (kaynak aralıkları ve mekânsal edit)
  -> render_profile (sunum ve encode ayarları)
  -> render_manifest (bütün girdi hash'leri)
  -> render review / final insan onayı
```

Faz 0 render ayarlarını sessizce değiştirme. Faz 0 değerlerini başlangıç
varsayılanı kabul eden, yalnızca izinli sunum anahtarlarını override eden açık bir
`effective_render_config` katmanı oluştur. Manifest; Faz 0 karar hash'i,
render-profile hash'i ve EDL/edit-decision hash'ini kaydetmeli.

## Teslimat 1 — Render Studio Hızlı Revize

### 1. Render profile sözleşmesi

- Slot bazlı, sürümlü ve atomik yazılan bir `render_profile` artefaktı oluştur.
- Mevcut `render_adjustment` verisini geriye uyumlu şekilde oku veya yeni profile
  bir kez taşı; aynı kavram için iki aktif doğruluk kaynağı bırakma.
- En az şu kontrollü alanları destekle:
  - çözünürlük preset'i;
  - kalite preset'i: `draft`, `preview`, `final`;
  - caption tema;
  - allowlist içinden font;
  - font boyutu/konumu için desteklenen kontrollü değerler;
  - aktif kelime vurgusu açık/kapalı ve vurgu rengi;
  - render modu;
  - frame zoom ve focus x/y.
- Kalite preset'lerini sunucuda gerçek width/height/encode seçeneklerine çöz.
  İstemci ham CRF/preset/codec argümanı göndermesin.
- Artefakta `schema_version`, `output_slot`, `base_plan_hash`, içerik hash'i,
  `updated_at`, `updated_by`, `reason`, `not_uploaded` ve gerekiyorsa önceki sürüm
  bağı ekle.
- Geçersiz alan, bilinmeyen font/preset ve slot taşması açık 4xx hatası vermeli.

### 2. Revizyon ve onay davranışı

Render profile kaydedildiğinde tek atomik servis işlemi şunları yapmalı:

1. Yeni profili doğrula.
2. Eski render klasörü/manifesti, render incelemesi/onayı ve publish intent'i
   mevcut revizyon arşivine taşı.
3. Script, voiceover, moment selection ve onaylı altyazı metnini koru.
4. Yeni profili yaz ve projeyi `render_required/review_required` durumuna getir.
5. Audit kaydına değişen alan adlarını, slotu, eski/yeni hash'i ve aktörü yaz;
   secret veya tüm serbest metni loglama.

Profil kaydı ile render başlatmayı ayrı işlemler olarak tut. Kullanıcı önce neyin
değişeceğini görebilmeli, sonra preview veya final render başlatabilmeli.

### 3. API

Mevcut API tarzına uygun slot bazlı read/update uçları ekle. Adlandırmayı repo
konvansiyonuna göre seç; semantik olarak şu işlemler bulunmalı:

- aktif/effective render profile getir;
- profile değişikliğinin arşiv etki planını getir;
- beklenen mevcut hash ile profile kaydet;
- profile varsayılanlarını Faz 0 ayarlarından yeniden oluştur;
- seçili profile ile preview/final render başlat.

Mümkünse stale browser yazımını engellemek için `expected_hash` veya eşdeğer
optimistic-concurrency kontrolü kullan.

### 4. Render Studio arayüzü

`Teknik özet` kartını belirsiz biçimde inputlara çevirmek yerine aynı panelde iki
açık bölüm göster:

- **Mevcut çıktı:** ffprobe/render manifestinden gelen salt okunur gerçekler.
- **Sonraki render:** düzenlenebilir render-profile değerleri.

Gerekli davranışlar:

- çözünürlük, kalite, caption tema/font, aktif kelime ve kadraj seçilebilir;
- değişen alanlar görsel olarak işaretlenir;
- kaydedilmemiş değişiklik ve stale render uyarısı görünür;
- `Ayarları kaydet`, `Hızlı preview`, `Final yeniden render`, `Geri al` ve
  `Video Editörde aç` aksiyonları bulunur;
- teknik özet yeni video üretilene kadar eski videonun gerçeklerini göstermeye
  devam eder; yeni seçimler mevcut çıktıymış gibi sunulmaz;
- mevcut narrated subtitle editor aynı akışta çalışmayı sürdürür;
- klavye kullanımı, label bağlantıları, loading/disabled/error durumları ve
  dar ekran düzeni test edilir.

### 5. Render entegrasyonu

- `_run_render` effective config'i merkezi resolver üzerinden alsın.
- Mevcut Faz 0 drift korumasını tamamen kaldırma; yalnızca allowlist içindeki
  profile override'larını meşru kabul edecek şekilde ayır.
- Render manifestine kullanılan profile/EDL hash'lerini ve çözümlenmiş gerçek
  ayarları yaz.
- Teknik özet manifestteki gerçek değerleri göstermeli.
- Caption tema/font/aktif kelime seçimi hem ASS hem fallback altyazı yollarında
  tutarlı olmalı.

## Teslimat 2 — Tek Klipli Video Editör

### 6. EDL/edit-decision sözleşmesi

- EDL'yi kaynak zaman aralıkları ve mekânsal editler için tek doğruluk kaynağı
  olarak geliştir; aynı alanları yeni bir artefakta tekrar etme.
- Her slot için EDL; kaynak fingerprint/hash'i, moment-selection hash'i,
  `source_duration_sec`, revision bilgisi ve clip listesini taşımalı.
- Yeni, açık bir `set_clip_source_bounds` benzeri işlem ekle. Bu işlem:
  - başlangıcın 0'dan küçük olmasını reddeder;
  - bitişin başlangıçtan büyük olmasını ister;
  - bitişi gerçek kaynak süresine göre sınırlar;
  - mevcut clip'in eski sınırlarının dışına uzatma ve taşıma yapabilir;
  - clip süresini koruyarak taşıma ile in/out handle değişimini ayırır.
- Eski `trim_clip` davranışını kullanan kod/testleri kırma; gerekirse eski metodu
  daraltma semantiğiyle koru ve editör için yeni metodu kullan.

### 7. Ana renderı EDL'ye bağla

- Seçilen momentten ilk slot EDL'sini deterministik üret.
- EDL mevcutsa ana render kaynak başlangıç/bitişini ondan alsın; yoksa mevcut
  moment davranışına geriye uyumlu fallback yap.
- `hot_moments` ve `moment_selection` değişmeden kalmalı.
- Manifestte gerçek kullanılan source in/out değerleri görünmeli.
- Direct/original-audio modunda clip zamanı değişirse eski transcript onayı ve
  render zinciri geçersiz olmalı; yeni aralık transkript edilip yeniden
  onaylanmadan final render açılmamalı.
- Narrated modda yalnızca görsel aralık değiştiğinde onaylı script/voice/subtitle
  korunmalı. Kullanıcı ayrıca `metni de uyarlat` kararı verirse script ve sonrası
  mevcut revizyon sistemiyle arşivlenmeli.

### 8. Video Editör ekranı

Ürün ekranını Creator Console'un mevcut arayüz sistemi içinde oluştur. Eski React
prototipini doğrudan ürün ekranı sayma; yeniden kullanım uygun değilse kaldırmak
yerine izole bırak ve gerçek akışı ana konsolda kur.

İlk sürüm özellikleri:

- slot seçimi ve ana kaynak video oynatıcısı;
- kaynağın tamamını temsil eden timeline;
- seçili clip aralığını gösteren bant;
- sürüklenebilir in/out handle'ları;
- orta alanı sürükleyerek süreyi koruyup clip'i taşıma;
- başlangıç/bitiş için `-1`, `-0.5`, `-0.1`, `+0.1`, `+0.5`, `+1` saniye;
- başlangıç, bitiş, clip süresi ve kaynak süresi göstergeleri;
- crop/zoom/focus kontrolleri;
- `Değişiklikleri kaydet`, `Önizle`, `Render Studio'ya dön` ve `Geri al`;
- geçersiz aralıkların istemcide ve sunucuda reddi;
- sayfa yenilemesinde kaydedilmiş editin geri gelmesi.

Timeline sürüklenirken FFmpeg çalıştırma. Kaynak video endpoint'i güvenli byte-range
seek destekliyorsa onu kullan; değilse mevcut medya servisinin güvenlik modeline
uygun range desteği ekle. Debounce sonrası veya kullanıcı `Önizle` dediğinde düşük
kaliteli preview üret. Aynı EDL/profile hash'i için preview cache kullanılabilir.

## Geçersiz kılma matrisi

| Değişiklik | Geçersiz olacaklar | Korunacaklar |
|---|---|---|
| Çözünürlük/kalite | render, render review/onayı, publish intent | moment, script, voice, altyazı metni |
| Caption tema/font/aktif kelime | render, render review/onayı, publish intent | altyazı metni ve zamanları |
| Crop/zoom/focus | render, render review/onayı, publish intent | içerik artefaktları |
| Altyazı metni | subtitle track'in önceki sürümü ve render sonrası | script/voice ve cue zamanları |
| Direct clip in/out | transcript onayı ve render sonrası | Faz 0 ve moment analiz kanıtı |
| Narrated clip in/out | render sonrası; görsel uyum tekrar incelenir | varsayılan olarak script/voice/subtitle |
| Narrated + metni uyarlat | script ve bütün sonrası | Faz 0 ve moment analiz kanıtı |
| Kaynak fingerprint değişimi | Faz 0 sonrası bütün zincir | yalnız proje kimliği/geçmiş |

## Test ve kabul kapıları

En az aşağıdaki otomatik kanıtları ekle:

- Render profile schema/default/migration/allowlist testleri.
- Slot izolasyonu: slot 1 revizyonu slot 2 artefaktlarına dokunmuyor.
- Profile kaydı yalnız render sonrası çıktıları arşivliyor; script/voice korunuyor.
- Aynı profile hash'iyle gereksiz arşiv veya render invalidation oluşmuyor.
- Stale `expected_hash` update'i reddediliyor.
- Direct clip aralığı değişince transcript onayı geçersizleşiyor.
- Narrated görsel aralık değişiminde script/voice dosya hash'leri korunuyor.
- Yeni bounds işlemi clip'i eski sınırların dışına uzatabiliyor ve kaynağı aşamıyor.
- Ana render EDL source in/out değerlerini gerçekten tüketiyor.
- Manifest dependency hash'leri ve teknik özet effective değerlerle eşleşiyor.
- Browser testinde Hızlı Revize alanları, kaydetme, stale uyarısı, sayfa yenileme,
  timeline handle/drag/nudge ve preview akışı doğrulanıyor.
- Eski lifecycle, subtitle editor, revision, render ve batch testleri yeşil kalıyor.

Gerçek medya doğrulaması ayrıca raporlanmalı:

- bir preview kalite render;
- bir 1080x1920 final render;
- font/aktif kelime değişikliğinin gözle görülmesi;
- clip'in kaynakta başka konuma taşındığının video ve manifest zamanlarıyla kanıtı;
- altyazı senkronu ve ses sürekliliğinin insan tarafından izlenmesi.

## Uygulama sırası ve durma noktaları

1. Artefakt sözleşmeleri, resolver ve invalidation testleri.
2. Hızlı Revize backend/API.
3. Hızlı Revize UI ve browser testleri.
4. EDL source-bounds ve ana render entegrasyonu.
5. Tek klip timeline UI ve preview.
6. Geniş regresyon ve gerçek medya raporu.

Her aşamayı yeşil bırakmadan sonrakine geçme. Çoklu clip sıralama, split, transition,
B-roll timeline ve müzik miks UI'sı bu görevin dışındadır; mevcut altyapıyı bozma,
yalnız sonraki M0.11 çalışması için uyumlu sınırlar bırak.

## Beklenen sonuç raporu

Son mesajda yalnız şunları ver:

1. Tamamlanan davranışlar.
2. Değişen dosyalar ve her birinin amacı.
3. Çalıştırılan testler ve net sonuçları.
4. Yapılamayan gerçek medya kontrolleri veya açık riskler.
5. Bir sonraki en küçük güvenli görev.

