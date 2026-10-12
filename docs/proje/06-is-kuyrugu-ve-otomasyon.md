# 06 · İş Kuyruğu, Worker ve Otomasyon

Kaynak: `@creator_console/jobs.py` (588
satır), `worker.py`, `progress.py`, `automation.py`, `quota_signals.py`

## Neden kuyruk var

Analiz, script üretimi, TTS ve render bir HTTP isteğinden **çok** daha uzun sürer.
UI bir **iş** gönderir ve onu yoklar. Bu yüzden doğruluk kaynağı tarayıcı belleği
değil, **diskteki iş durumudur**; sayfa yenilemesi, backend restart'ı ve worker
çökmesi işi kaybettirmemelidir.

## Dört garanti

### 1. Idempotency

Gönderim `(proje, job_type, girdiler)`'den türetilmiş bir `idempotency_key`
taşır. Eşdeğer bir iş `queued`/`running`/bitmiş durumdayken aynı anahtarla
gönderim **o işi döndürür**, ikincisini oluşturmaz. Çift tıklama veya yeniden
denenen bir POST iki sağlayıcı çağrısına yol açamaz.

### 2. Tek claim

Worker bir işi çalıştırmadan önce `claim()` etmek zorundadır. Claim, proje
bazında dosya kilidi altında **compare-and-set**'tir; iki eşzamanlı worker aynı
işi asla ikisi birden çalıştıramaz.

### 3. Lease (kiralama)

Claim `lease_expires_at` taşır. Ölen bir worker **süresi geçmiş** bir lease
bırakır; `recover_abandoned()` işi `running`'de sonsuza kadar bırakmak yerine
`failed` + `recoverable=True`'ya taşır. Varsayılan
`DEFAULT_LEASE_SECONDS = 900` (15 dk), `MAX_ATTEMPTS = 3`.

### 4. Atomik durum

Her yazım `atomic_write_json` üzerinden gider; çökme yarım yazılmış ve sonradan
çöp olarak ayrıştırılan bir iş kaydı bırakamaz.

## Yürütme durumu ≠ artefakt durumu

Bu ayrım bilinçlidir:

| Alan | Anlamı |
|---|---|
| `status` | **Yürütme** durumu: `queued`, `running`, `review_required`, `failed`, `completed`, `cancelled` |
| `artifact_status` | Pipeline'ın yazdığı şey: `review_required`, `insufficient_data`, `failed`, `approved`… |

`ACTIVE_STATUSES = (queued, running)`, kalanlar terminaldir.

## İş türleri

Serbest biçimli "rastgele kod çalıştır" işi **yoktur**. Her tür
`pipeline_service` içindeki tek bir kanonik giriş noktasına eşlenir:

```
source_ingest · source_assessment · video_analysis · transcript ·
script_generation · tts · render · metadata_generation ·
publish_upload · visibility_change
```

## Ayrı süreçli worker

```bash
python -m creator_console.worker --storage-root ./.console_data --project-id prj_xxx
```

- Bağımsız bir **OS sürecidir**; işleri aynı compare-and-set + lease ile claim
  eder.
- Çalışırken lease'i **heartbeat** ile uzatır.
- Başlangıç kurtarması **yapmaz**.
- Sunucu yeniden başlatıldığında yalnızca sahibi kanıtlanabilir şekilde ölmüş
  işler kurtarılır; **canlı bir worker'ın işi elinden alınmaz**.

`--inline-jobs` bayrağı işleri arka plan thread'i yerine senkron çalıştırır
(hata ayıklama için).

## Progress (ilerleme)

`progress.py` (434 satır) — uzun fazlar için **sunucuda kalıcı** ilerleme.
Yüzde/aşama bilgisi tarayıcı belleğinde tutulmaz; sayfa yenilendiğinde veya başka
bir sekmeden bakıldığında aynı ilerleme görünür. `GET /progress` ile okunur.

## Kota sinyalleri: "sağlayıcı hayır dedi" ne demek?

`quota_signals.py` bir sağlayıcı reddini **zamanlama kararına** çevirir. Bunun
paylaşılmış olması bilinçli bir karardır; modülün kendi notu, ilk canlı uzun-form
pilotunda bir 429'un `failed` olarak kaydedildiğini, bunun da yeniden denemeye
davet çıkardığını ve **yeni bitmiş kotadan daha fazla harcadığını** anlatır.

| Sinyal | Doğru tepki |
|---|---|
| Dakika başına limit (RPM) | Bir dakika bekle |
| Günlük limit (RPD) | Yarın gel; şimdi denemek kalanı yakar |
| Faturalandırma/plan bloğu | Beklemek çözmez; bir insan ayar değiştirmeli |
| Diğer | Gerçek hata; öyle raporla |

Sınıflandırma kararı burada yaşar ve **her workflow aynı soruyu buna sorar** —
ikinci bir kopya sapar ve sapma yalnızca üretimde görünür. Fonksiyonlar hiçbir
şey kalıcılaştırmaz ve kimin sorduğunu bilmez.

Karşılık gelen aşama durumları `stage_machine` içinde:
`waiting_rate_limit`, `waiting_daily_quota`, `blocked_billing`.

## Otomatik akış

`automation.py` (493 satır) — **sunucuda kalıcı** otomatik akış
(`results/automation.json`).

Bir proje açıkken "Otomatik akışı başlat" sıradaki güvenli **makine** fazını
çalıştırır. İnsan kararı gereken noktalarda (plan türü, moment seçimi, hash
onayları) durur; kullanıcı işlemi sonrası aynı düğmeyle devam eder. Kaynak
değerlendirme, video analizi, script, TTS ve render ayrıca kendi sekmelerinden
manuel olarak yeniden çalıştırılabilir.

İlgili uç noktalar: `GET/POST /automation`, `POST /auto-run`.

## Preflight ve tanılama

- `status.py` — yerel ön kontroller: zorunlu modüller (`faster_whisper`,
  `scenedetect`, `librosa`, `numpy`, `imageio_ffmpeg`), opsiyonel modüller
  (`playwright`, `google.genai`), en az **500 MB** boş disk
  (`MIN_FREE_DISK_MB`). Sonuç `ok` / `warn` / `fail`.
- `status.build_workflow_map()` — 10 adımlı iş akışı haritası; adım durumları
  `completed`, `current`, `waiting`, `blocked`, `failed`, `skipped`. Bir faz
  başarısız olup sonra başarıyla yeniden koştuysa **artık başarısız gösterilmez**
  (`_latest_failures` yalnızca en yeni işi dikkate alır) — kullanıcı çözülmüş bir
  sorunu düzeltmeye gönderilmez.
- `diagnostics.py` (879 satır) — işlevsel tanılama: "her kullanıcıya dönük
  yetenek gerçekten çalışıyor mu?" `GET/POST /api/diagnostics`.

## Hata sözleşmesi

`errors.py` (221 satır) — **kapalı katalog** yapısal hatalar. Yanıt gövdesi:

```json
{"error": {"code": "missing_required_field",
           "message": "Zorunlu alan(lar) bos.",
           "hint": "Yildiz (*) ile isaretli alanlari doldurup tekrar dene.",
           "retryable": false,
           "debug_id": "dbg_d174a4a629cd",
           "fields": [{"name": "operator_name", "label": "Proje ureticisi"}]}}
```

- `debug_id` sunucu günlüğündeki ayrıntıyla eşleşir; **iç ayrıntı gövdeye
  konmaz**.
- Katalog mesajları statiktir (`verify_faults.py` bunu test eder).
- `redaction.py` süreç-yerel sır maskeleme kaydı tutar; sır bir istisna metnine,
  sunucu günlüğüne veya iş kaydına düşmez — bu da fault-injection suitinde
  doğrulanır.
