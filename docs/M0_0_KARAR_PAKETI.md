# M0.0 — Karar paketi

**Hazırlanma tarihi:** 2026-08-23
**Amaç:** M0.0'ın kod olmayan üç maddesi için **onaylanacak somut metin** üretmek.

Bu belge karar vermez. Kodda ve `ROADMAP.md`/`MILESTONES.md`'de hâlihazırda ne
olduğunu çıkarır, boşlukları işaretler ve senin işaretleyeceğin hâle getirir.
Onaylanan bölümler MILESTONES'a kanıt olarak geçer.

Kaynaklar: `ROADMAP.md`, `local_pipeline/publish.py`, `local_pipeline/turbo.py`,
`local_pipeline/source_strategy.py`, `MILESTONES.md`.

---

## 1. MVP PRD ve kabul kriterleri (taslak — onayın gerekiyor)

### 1.1 Ürün tanımı

Kullanıcının **kendi verdiği** kaynak videodan, insan onaylı adımlarla 9:16
Short üreten yerel-öncelikli bir konsol. Araştırma yan kolu karar desteği verir;
üretim girdilerini kendiliğinden değiştirmez.

### 1.2 Değişmez sınırlar (ROADMAP'ten, değiştirilmedi)

- Ana hat yalnızca kullanıcının verdiği kaynak medyayı işler.
- `kaynak → analiz → moment → (script/seslendirme) → 9:16 render → insan onayı`.
- **Hiçbir çıktı kendini onaylamaz veya otomatik yayımlanmaz.**
- Tek bir AI/TTS sağlayıcısına bağımlılık kurulmaz.
- Tahminler kanıt ve güven seviyesi taşır; izlenme garantisi verilmez.

### 1.3 MVP kapsamına GİREN

| Yetenek | Durum |
|---|---|
| Faz 0 kaynak değerlendirme + mod seçimi (`direct_clip_series` / `narrated_theory`) | Çalışıyor |
| Telif / dönüştürücü kullanım kontrol listesi (plan onayı kapısı) | Çalışıyor |
| Yerel analiz, hot moment, moment seçimi | Çalışıyor |
| Altyazı transkripti + insan düzeltmesi + sözlük | Çalışıyor |
| Script Studio + TTS + dinleme listesi | Çalışıyor |
| 9:16 render (önizleme **ve** final 1080×1920) | Çalışıyor |
| Render inceleme listesi + hash bağlı onay | Çalışıyor |
| Private upload + ayrı görünürlük onayı | Kod var, **gerçek OAuth denenmedi** |

### 1.4 MVP kapsamına GİRMEYEN (bilinçli)

- Otomatik yayın, zamanlama, çoklu kanal yönetimi.
- Sistemin telif hakkında hukuki bir sonuca varması.
- Yüz tanıma (M0.11'de kapsam dışı bırakıldı).
- SaaS/ölçek (M2.0, ertelendi).

### 1.5 Kabul kriterleri — ölçülmüş olanlar

| Kriter | Kanıt |
|---|---|
| Hiçbir artefakt kendini onaylamıyor | `verify_faults.py` 70/70 |
| Onaylar içerik hash'ine bağlı, üst-akış değişince düşüyor | `verify_console.py` |
| İnsan kapıları gerçekten kapalı | `verify_short_poc.py` 31/31 |
| Final 1080×1920 render + ses akışı | `verify_short_poc.py` |
| Telif kapısı cevaplanmadan plan onaylanmıyor | `verify_copyright_gate.py` 26/26 |
| Sır loga/artefakta/audit'e sızmıyor | `verify_faults.py`, `verify_secret_entry.py` |
| Otomatik yükleme yok | `verify_short_poc.py`, `verify_console.py` |

### 1.6 Kabul kriterleri — **insan onayı bekleyen**

- [ ] Üretilen Short baştan sona izlendi ve yayınlanabilir bulundu.
- [ ] Altyazı okunabilirliği ve konuşma senkronu kabul edildi.
- [ ] Türkçe telaffuz ve ses dengesi kabul edildi.
- [ ] Gerçek OAuth ile private upload denendi ve YouTube Studio'da doğrulandı.

> **Onayın:** Yukarıdaki 1.1–1.6 MVP tanımını ve kabul kriterlerini onaylıyor
> musun? Değiştirmek istediğin madde varsa burada işaretle.

---

## 2. Private upload yetkilendirme sınırı (koddan çıkarıldı — teyidin gerekiyor)

Bu bölüm bir öneri değil; `local_pipeline/publish.py` ve `publish_youtube.py`'nin
**fiilen uyguladığı** sınırın yazılı hâli.

### 2.1 Sınır

1. **İlk yükleme her zaman `private`.** `INITIAL_PRIVACY = "private"`; çağıran ne
   isterse istesin hiçbir sağlayıcı ilk yüklemeyi başka görünürlükle yapamaz.
2. **Görünürlük değişimi ayrı bir insan kararıdır.** `unlisted` ve `public`,
   `AUDIENCE_EXPOSING_VISIBILITIES` olarak işaretli ve kendi onayını ister.
3. **Tek doğruluk kaynağı `validate_publish_preconditions()`.** Kaydedilmiş bir
   bayrağa güvenmez; render onayını yeniden türetir.
4. **Üçüncü taraftan alınan kaynakta hak kaydı yoksa yayın reddedilir.**
5. **Her kapı, insanın incelediği şeyin SHA-256'sına bağlıdır.** Onaydan sonra
   metadata düzenlenirse o onay düşer.
6. **Bu modül ağa çıkmaz ve sır okumaz**; onu sağlayıcılar yapar.

### 2.2 Yazılı tanım (onaylanacak metin)

> Bu ürün, kullanıcının açık ve ayrı kararı olmadan hiçbir içeriği bir izleyici
> kitlesine açmaz. Yetkilendirme sınırı şudur: sistem, kullanıcı adına **yalnızca
> `private` görünürlükte** yükleme yapabilir. `unlisted` veya `public`'e geçiş,
> yüklemeden bağımsız, içerik hash'ine bağlı ikinci bir insan onayı gerektirir ve
> bu onay içerik değişirse otomatik olarak düşer.

### 2.3 Açık nokta

- Gerçek OAuth hesabıyla hiç denenmedi (M0.7). Sınır kodda uygulanıyor ama
  canlı ortamda gözlenmedi.

> **Onayın:** 2.2'deki tanımı onaylıyor musun?

---

## 3. Short türü önceliklendirme (envanter — önceliği sen vereceksin)

Kodda **fiilen** desteklenen türler:

### 3.1 Ana hat modları (`source_strategy`)

| Mod | Ne yapar | Sağlayıcı maliyeti |
|---|---|---|
| `direct_clip_series` | Kaynağın kendi sesi + düzeltilmiş altyazı | **Sıfır** (script/TTS kapalı) |
| `narrated_theory` | Yazılan anlatım + TTS; kaynak b-roll | Script + TTS |

### 3.2 Turbo içerik türleri (`turbo.CONTENT_TYPES`)

`explainer`, `listicle`, `story`, `news_recap`, `tutorial`

### 3.3 MILESTONES'ta adı geçen ama kodda **karşılığı olmayan** türler

- `recap` — `news_recap` Turbo'da var, ana hatta ayrı bir mod yok.
- `scene explanation` — `narrated_theory` ile yapılabilir ama kendi türü yok.
- `viral / analitik / sinematik varyantlar` — M0.3'te açık madde, uygulanmadı.

### 3.4 Karar gereken

> Hangi tür MVP'nin **birincil** çıktısı olsun? Öneri sunmuyorum; bu bir ürün
> kararı ve kullanıcı kitleni benden iyi biliyorsun. Yalnızca şunu belirteyim:
> `direct_clip_series` sıfır sağlayıcı maliyetiyle çalışıyor ve kota riski yok;
> `narrated_theory` her Short için script + TTS kotası harcıyor.

Sıralaman:

1. _______________
2. _______________
3. _______________

---

## Sonraki adım

Onayladığın bölümler MILESTONES'taki M0.0 maddelerine kanıt olarak işlenir.
Onaylamadıkların açık kalır — kod hazır olması bir kararı kapatmaz.
