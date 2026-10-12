# 08 · Render, Scene Editor ve Render Atölyesi

Kaynak: `@local_pipeline/render_utils.py`
(1835 satır), `scene_editor.py` (618), `timeline.py` (653),
`creator_console/render_profile.py`, `apps/web/src/screens/RenderAtelierScreen.jsx`

## Görev dağılımı

| Modül | Sorumluluk |
|---|---|
| `render_utils.py` | 9:16 yeniden çerçeveleme, ses mixi/ducking, altyazı yazımı, ffprobe/ffmpeg fallback ile teknik doğrulama, tam render + onay kapısı |
| `timeline.py` | "Klip listesi + anlatım → tek video" ortak motoru (Turbo ve Long-form tarafından paylaşılır) |
| `scene_editor.py` | **EDL** (Edit Decision List) modeli ve preview derleyicisi |
| `render_profile.py` | Faz 0 sonrası değiştirilebilen sunum ayarları (slot başına) |

`scene_editor` mevcut render kodunu yeniden yazmaz; `render_utils`'in hazır
grafiğini "klip listesi" kavramına genişletir.

## Dikey yeniden çerçeveleme

`build_vertical_filtergraph(mode, width, height, ...)` **tam bir ifade** döner —
kendi giriş ve çıkış etiketlerini adlandırır. Varsayılanla `[0:v]` → `[framed]`
üretir.

İki mod:

- **`center_crop`** — `scale=W:H:force_original_aspect_ratio=increase` +
  `crop=W:H:(iw-ow)*ax:(ih-oh)*ay`
- **`blurred_background`** — `split=2` → arka plan `increase` ile ölçekle, kırp,
  `gblur` uygula; ön plan `decrease` ile ölçekle; `overlay` ile birleştir.
  **Beş** etiket üretir (`bg_src`, `fg_src`, `bg`, `fg`, çıkış).

`in_label` / `out_label` / `label_suffix` parametreleri **tam da bu yüzden**
vardır: N klibi tek grafikte birleştirmek bu zincirin N kopyasını gerektirir ve
her etiket tekil olmalıdır. `blurred_background` beş etiket ürettiği için yalnızca
giriş/çıkışı yeniden adlandıran bir çağıran `bg_src`, `fg_src`, `bg`, `fg`
üzerinde **hâlâ çakışır** — `label_suffix` dördüne de eklenir.

> **Hata kaydı:** `build_preview_filtergraph` başlangıçta bu parametreleri
> kullanmıyordu ve her klip için sabit `[0:v]`/`[framed]` üretiyordu. Çok klipli
> grafik `[preclip_0][0:v]…[framed][framed_0]` gibi geçersiz bir ifadeye
> dönüşüyordu. Düzeltme: `@local_pipeline/scene_editor.py:440-446`.
> Regresyon testi: `test_preview_filtergraph_labels_are_unique`.

## Kalite ve çözünürlük ön ayarları

```
QUALITY_PRESETS = {
  'draft':   {'scale': 0.35, 'crf': 32, 'encode_preset': 'ultrafast'},
  'preview': {'scale': 0.50, 'crf': 30, 'encode_preset': 'ultrafast'},
  'final':   {'scale': 1.00, 'crf': 18, 'encode_preset': 'slow'},
}
DELIVERY_QUALITY = 'final'
RESOLUTION_PRESETS = {'vertical_1080x1920', 'vertical_720x1280'}
RENDER_MODES = ('blurred_background', 'center_crop')
```

Altyazı stilleri: `active_word_yellow` (dinamik, aktif kelime vurgulu) ve
`static_block`. Aktif renk seçenekleri ASS biçiminde:
`yellow = &H0000FFFF`, `white = &H00FFFFFF`, `cyan = &H00FFFF00`.

`OVERRIDABLE_RENDER_KEYS` — plan onayından sonra da değiştirilebilenler:
`render_mode`, `preview_mode`, `caption_style`, `caption_theme`.

## EDL veri modeli

`results/edl/edl.json` → `artifact_type: edl`, zorunlu alanlar
`artifact_type, status, title, width, height, clips`.

```
EDL
├── title, width=1080, height=1920, render_mode='center_crop'
├── music_track: str | None
├── revision_notes, revision_requested_by      ← insan geri bildirimi
├── created_at (UTC ISO), version
└── clips: [EDLClip]
        ├── clip_id, source_path
        ├── source_start_sec, source_end_sec, timeline_start_sec
        ├── crop: Box(x, y, w, h)              ← normalize 0..1
        ├── zoom: Zoom(scale>=1, anchor_x, anchor_y)
        ├── caption_style: CaptionStyle
        ├── b_roll: [BRoll]
        └── notes
```

### `CaptionStyle`

| Alan | Varsayılan | Not |
|---|---|---|
| `preset` | `active_word_yellow` | `classic_white`, `lower_third` de geçerli |
| `font_size` | 24 | |
| `margin_v` | 50 | |
| `word_color` | `&H00FFFFFF` | ASS `&HAABBGGRR` |
| `active_color` | `&H0000FFFF` | aktif kelime (sarı) |
| `position_x` / `position_y` | 0.5 / 0.9 | **normalize serbest konum** |
| `outline_color` / `box_color` | `&H00000000` | |
| `alignment` | `center` | `left`, `right` |
| `font` | `DejaVu Sans` | |
| `shadow` / `border_width` / `uppercase` | false / 0 / false | |
| `sample_text` | `Caption Ornegi` | önizlemede çizilen metin |

`is_valid()` preset'i, 0..1 konum aralığını, hizayı, boş olmayan fontu ve negatif
olmayan çerçeveyi doğrular.

### `BRoll`

`media_path`, `start_sec`, `duration_sec`, `position` (`overlay` / `split_screen`),
`opacity` (0..1), `position_x`, `position_y` (0..1).

### Klip sınırları: `trim_clip` vs `set_clip_source_bounds`

Bu iki fonksiyonun ayrı olması bilinçlidir:

- **`trim_clip`** yalnızca **daraltır**: mevcut başlangıçtan önce bir başlangıcı
  ve mevcut bitişten sonra bir bitişi reddeder. "Trim" için doğrudur ama seçilen
  aralığı kalıcı hale getirir.
- **`set_clip_source_bounds`** düzenlenebilir sürümdür: klibi kaynağın
  **herhangi** bir yerine taşır/boyutlandırır. Tek sınırlar kaynağın kendisi ve
  süre tabanıdır (`MIN_CLIP_DURATION_SEC = 0.2`) — çünkü sıfır uzunluklu klip bir
  düzenleme değil, bozuk bir render'dır.
- **`shift_clip`** süreyi **koruyarak** kaydırır. "Moment bir saniye geç
  başlıyor" ile "moment bir saniye uzun olmalı" farklı düzenlemelerdir.

Gerekçe: operatör momentin bir saniye geç başladığını fark ettiğinde geri
uzanamıyordu; tek yol analizi yeniden koşup başka moment seçmekti, bu da
transcript'i ve onunla birlikte script'i çöpe atıyordu.

## Preview filtergraph derleyicisi

`build_preview_filtergraph(edl) -> (filter_complex, total_input_count)`

Klip başına üretilen zincir:

```
1) [N:v] crop=iw*w:ih*h:iw*x:ih*y , scale=(W*zoom):(H*zoom):flags=lanczos  → [preclip_N]
2) [preclip_N] <vertical filtergraph, label_suffix=_N>                      → [framed_N]
3) her B-roll için:
   [M:v] scale=iw*0.3:-2:flags=lanczos, format=rgba,
         colorchannelmixer=aa=<opacity>                                     → [ol_M]
   [base][ol_M] overlay=main_w*px-overlay_w/2 : main_h*py-overlay_h/2        → [tmp_N_M]
4) [base] scale=W:H, setsar=1, fps=30, format=yuv420p                       → [norm_N]
```

Ardından:

```
5) [norm_0][norm_1]…concat=n=N:v=1:a=0                                      → [concatv]
6) [concatv] format=yuv420p                                                 → [vtmp]
7) [vtmp] drawtext=text='…':x=…:y=…:font=…:fontsize=…:fontcolor=0xRRGGBB
          :box=0|1:boxcolor=…[:borderw=…:bordercolor=…][:shadowx=2:shadowy=2] → [vout]
```

`caption_style.sample_text` boşsa 7. adım atlanır ve `[concatv]` doğrudan
`[vout]` olur.

### Neden `setsar=1, fps=30` normalizasyonu var

`concat` filtresi boyut, **SAR**, pixel format veya kare hızı farklı bağlantıları
reddeder — ve klip başına farklı crop/zoom katsayıları **gerçekten** farklı SAR
üretir. Gerçek koşuda alınan hata:

```
[Parsed_concat_12] Input link in0:v0 parameters (size 1080x1920, SAR 256:81)
do not match the corresponding output link in0:v0 parameters (1080x1920, SAR 2048:729)
```

Düzeltme `@local_pipeline/scene_editor.py:466-475`
(`PREVIEW_FPS = 30`). Regresyon testi:
`test_preview_filtergraph_normalises_streams_before_concat`.

### Renk dönüşümü

`_ass_to_rgb()` ASS `&HAABBGGRR` / `&HBBGGRR` değerini ffmpeg `0xRRGGBB`'ye
çevirir (bayt sırası ters). `_drawtext_x_expr()` hizaya göre x ifadesini üretir:

| `alignment` | x ifadesi |
|---|---|
| `left` | `w*px` |
| `center` | `w*px-text_w/2` |
| `right` | `w*px-text_w` |

## Komut kurulumu ve girdi sırası

`build_preview_command(edl, output_path)`:

1. Önce **tüm klip girdileri**: her biri için `-ss <start> -t <duration> -i <path>`
2. Sonra **tüm B-roll girdileri**: `-ss <start_sec> -t <duration_sec> -i <media>`
3. `-filter_complex <graph> -map [vout] -an -c:v libx264 -pix_fmt yuv420p
   -movflags +faststart <out>`

Filtergraph'taki `broll_input_idx` sayacı da klip sırasına göre arttığı için girdi
indeksleri komuttaki sırayla **birebir** eşleşir. Önizleme şimdilik sessizdir
(`-an`).

`render_preview()` komutu çalıştırır ve `{command, returncode, stdout, stderr}`
döner; sıfır olmayan dönüş kodunun hata olup olmadığına **çağıran** karar verir
(test ortamında kaynak medya eksik olabilir).

## Render Atölyesi (React)

`@apps/web/src/screens/RenderAtelierScreen.jsx`

Canva benzeri, sürükle-bırak bir editör:

- **9:16 tuval** (360×640 px önizleme) üzerinde üç sürüklenebilir katman:
  **crop çerçevesi**, **altyazı** ve her **B-roll** kutusu.
- Sürükleme normalize koordinata çevrilir ve **bırakıldığında** `PATCH
  /edl/clips/{i}` ile sunucuya yazılır (sürükleme sırasında yerel state).
- Özellikler paneli seçime göre değişir: klip (başlangıç/bitiş/notlar), crop,
  zoom, altyazı (metin, renkler, font, boyut, hiza, çerçeve, gölge, büyük harf,
  preset) veya B-roll (medya, başlangıç, süre, opaklık, konum).
- EDL düzeyi: başlık, render modu, müzik.
- **Revizyon talebi**: serbest metin not `POST /edl/revision` ile gönderilir;
  atölye `revision_notes` + `revision_requested_by` alanlarını gösterir ve EDL
  sürümü artar.
- `creator_console/pipeline_service.py`de ayrıca `resolve_edl_revision`
  bulunur (notu/isteyeni temizler, yeni versiyon yazar ve `edl.revision_resolved`
  audit kaydı atar), ancak `creator_console/api.py`de henüz bir uç noktası yok.
- "Preview Komutu" ffmpeg komutunu gösterir, "Render Al" gerçek ffmpeg'i çalıştırıp
  `returncode` + `stderr` döker.

API kökü `import.meta.env.VITE_API_ROOT ?? ''` — göreli, yani Vite proxy'si
kullanılır. Backend'de CORS middleware olmadığı için mutlak
`http://127.0.0.1:8000` kökü tarayıcıdan çalışmaz.

## Doğrulanmış davranış

Gerçek ffmpeg ile uçtan uca koşu (2 klip + B-roll + altyazı):

| Ölçüm | Sonuç |
|---|---|
| `returncode` | 0 |
| Çıktı | 1080×1920, 30 fps, SAR 1:1, ~4.93 sn |
| Altyazı | 0.4/0.75 konumunda **646** sarı piksel |
| B-roll | 0.75/0.25 konumunda **2916** kırmızı piksel (0.7 opaklıkta harmanlanmış) |
| Arka plan | (0, 0, 253) — kaynak rengi korunmuş |

`fontconfig` uyarısı (`Cannot load default config file`) imageio-ffmpeg
derlemesinden gelir ve **ölümcül değildir**; `drawtext` yine çizim yapar.

## Bilinen sınırlar

- Önizleme **sessizdir** (`-an`); `music_track` EDL'de taşınır ama preview
  komutuna miksleme yapılmaz.
- `drawtext` tek bir `sample_text` çizer — kelime kelime zamanlama render
  hattındadır (`render_utils`), EDL önizlemesinde değil.
- `BRoll.position` alanı `split_screen` değerini kabul eder ama preview derleyicisi
  yalnızca `overlay` düzeni üretir.
- B-roll ölçeği sabit `iw*0.3`'tür; henüz düzenlenebilir değildir.
