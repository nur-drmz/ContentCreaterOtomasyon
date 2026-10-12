# 03 · Depolama ve Artefaktlar

Kaynak: `@creator_console/storage.py`,
`@creator_console/ids.py`,
`@creator_console/pipeline_service.py`

## Namespace izolasyonu

Konsolun yazdığı **her şey** tek bir ağaç altında yaşar:

```
<storage-root>/                     # varsayılan ./.console_data
  workspaces/
    <workspace_id>/
      .trash/<project_id>__<UTC>__<rand>/     # "sil" = geri alınabilir taşıma
      projects/
        <project_id>/
          project.json
          source_videos/
          results/
          jobs/
          progress/
          .runtime/
          audit_events.jsonl
```

`LocalFilesystemStorage.resolve()` göreli bir adı gerçek yola çeviren **tek**
yoldur ve namespace dışını döndürmeyi reddeder. Reddedilenler *sanitize edilip
kabul edilmez*, hata olarak yükselir:

- `../` gezinme (`StorageViolation: yol gezinme denemesi reddedildi`)
- mutlak yollar, Windows sürücü harfi (`C:`), UNC (`//sunucu`)
- symlink ile namespace dışına çıkma (`os.path.realpath` ile kontrol)
- güvensiz dosya adları (`safe_filename` ile normalize, ardından doğrulama)

`relative_to_namespace()` tarayıcıya **mutlak host yolu döndürmez**; operatörün
dizin düzenini sızdırmamak için namespace'e göreli POSIX string üretir.

## Atomik yazma

`atomic_write_bytes()` aynı dizinde geçici dosya + `os.replace` kullanır:

- POSIX ve Windows'ta (aynı dosya sistemi) atomiktir → çökme anında yarı yazılmış
  manifest görünmez; okuyucu ya eski ya yeni belgeyi görür.
- Geçici dosya adı **pid + thread id** taşır: aynı artefaktı eşzamanlı yazan iki
  thread aynı temp dosyayı paylaşıp silmez.
- Windows'ta başka bir handle açıkken `os.replace` `PermissionError` verir. Bu
  geçici çekişme olduğu için **jitter'lı geri çekilme** ile 25 kez denenir
  (`_REPLACE_ATTEMPTS`, 10 ms → 250 ms tavan). Bütçe biterse hata **yükseltilir**
  — çağıran asla "yazıldı" bilgisiyle yanıltılmaz.
- Bozuk JSON okunduğunda makul görünen bir varsayılanla **sessizce
  değiştirilmez**; hata çağırana bildirilir.

## Artefakt kayıt defteri

`pipeline_service.ARTIFACTS` — mantıksal ad → namespace içi yol (kayıt defterinin
asıl tanımı `application/artifact_catalog.py`'dedir; `pipeline_service` yeniden
dışa aktarır):

| Anahtar | Yol |
|---|---|
| `project` | `project.json` |
| `source_candidates` | `results/source_candidates.json` |
| `rights_decision` | `results/rights_decision.json` |
| `ingest_manifest` | `results/ingest_manifest.json` |
| `source_assessment` | `results/source_assessment.json` |
| `processing_plan` | `results/processing_plan.json` |
| `hot_moments` | `results/hot_moments.json` |
| `moment_selection` | `results/moment_selection.json` |
| `moment_transcript` | `results/moment_transcript.json` |
| `selected_script` | `results/selected_script.json` |
| `selected_voiceover` | `results/selected_voiceover.json` |
| `subtitle_track` | `results/subtitle_track.json` |
| `glossary` | `results/glossary.json` |
| `edl` | `results/edl/edl.json` |
| `render_manifest` | `results/render/render_manifest.json` |
| `publication_metadata` | `results/publication_metadata.json` |
| `publish_intent` | `results/publish_intent.json` |
| `publish_manifest` | `results/publish_manifest.json` |
| `long_form_plan` | `results/long_form_plan.json` |
| `turbo_brief` | `results/turbo/brief.json` |
| `turbo_workflow` | `results/turbo/workflow.json` |
| `turbo_timeline` | `results/turbo/timeline.json` |

Ek inceleme artefaktları modüllerinde tanımlıdır:
`results/render_review.json`, `results/voice_review.json`,
`results/copyright_review.json`, `results/automation.json`,
`results/long_form/workflow.json`, `results/turbo/…` (script, media_plan,
tts_chunks, narration.wav, subtitles.srt/.ass).

Sabit dizin adları: `SOURCE_DIRNAME = source_videos`,
`RESULTS_DIRNAME = results`, `RUNTIME_DIRNAME = .runtime`,
`JOBS_DIRNAME = jobs`.

## Şema doğrulama

- `SUPPORTED_SCHEMA_VERSIONS = {1, 2}` — bilinmeyen sürüm sessizce okunmaz.
- `REQUIRED_FIELDS` her artefakt türü için zorunlu alan demetini tanımlar.
  Örnekler:
  - `edl` → `artifact_type, status, title, width, height, clips`
  - `source_assessment` → `artifact_type, status, source, mode_scores`
  - `publish_manifest` → `artifact_kind, status, platform, upload`
- `PRODUCTION_READY_ALLOWED` — hangi artefaktların bu duruma çıkabileceğini
  sınırlar.

## Çoklu çıktı (batch) yerleşimi

Bir plan 1'den fazla Short üretiyorsa:

- **Slot 1** tarihsel `results/render/` yolunu korur.
- **Slot 2..N** kendi `results/render/output_NN/` dizinine yazar.

Böylece bir Short diğerinin videosunu, altyazısını veya manifestini ezemez. Aynı
moment iki yuvada seçilemez; her yuva kendi 6 maddelik inceleme listesini ve
kendi onayını taşır (`PER_SLOT_ARTIFACTS`).

## Geri alma (rewind)

`REWIND_PATHS` her geri alma noktası için **silinecek** artefakt/dizin listesini
tutar; `REWIND_LABELS` kullanıcıya gösterilen adı verir:

| Anahtar | Etiket |
|---|---|
| `assessment` | Faz 0 degerlendirme |
| `plan` | isleme plani |
| `analysis` | video analizi |
| `moment` | moment secimi |
| `script` | script |
| `voice` | seslendirme |
| `render` | render |

Mantık kademelidir: `assessment`'a dönmek plan, analiz, moment, script, ses,
render, review ve progress artefaktlarının tümünü temizler — yani eski bir onay
yeni bir zincire miras kalamaz.

## Silme = çöp kutusu

`trash_namespace()` projeyi `workspaces/<ws>/.trash/<project>__<zaman>__<rand>`
altına **atomik olarak taşır**. UI buna "sil" der ama baytlar kurtarılabilir
kalır. Hedef yalnızca doğrulanmış ownership bileşenlerinden türetilir; çağıran
yol, glob veya üst dizin veremez. `project_id` olmadan silme reddedilir.

## Audit izi

`creator_console/audit.py` — proje namespace'i başına bir append-only JSONL
(`audit_events.jsonl`). Olay türü **kapalı sözlüktür**; `EVENT_TYPES` içinde
olmayan bir tür `ValueError: bilinmeyen audit olayi` ile reddedilir (bu bilinçli:
bilinmeyen olay bir hatadır, serbest metin alanı değil).

Katalog **41 olay** içerir. Aileler: `project.*`, `plan.*`, `job.*`, `artifact.*`,
`approval.*`, `clip_bounds.saved`, `render_profile.*`, `settings.changed`,
`provider.call`, `transcript.manual_edits_archived` ve `edl.*` (`created`,
`updated`, `clip_added`, `music_set`, `revision_requested`,
`revision_resolved`).

> **Not:** `edl.*` olayları başlangıçta katalogda yoktu; sonuç olarak sahne
> editörünün her yazımı `validation_failed` ile 400 dönüyordu. Katalog artık bu
> aileyi içerir — yeni bir `audit.record()` çağrısı eklerken olay adını
> `EVENT_TYPES`'a eklemek zorunludur.
