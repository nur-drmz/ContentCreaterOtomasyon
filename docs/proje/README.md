# YoutubeOtomationv0 — Proje Dokümantasyonu

Bu klasör, projenin **kodda gerçekten var olan** davranışını belgeler. Her iddia
kaynak dosya ve satır aralığına dayanır; planlanan ama yazılmamış şeyler
"planlandı" olarak ayrıca işaretlenir.

## Okuma sırası

| # | Dosya | Ne anlatır |
|---|---|---|
| 01 | [`01-genel-bakis.md`](01-genel-bakis.md) | Ürün nedir, değişmez sınırlar, kalite/politika sözleşmesi |
| 02 | [`02-mimari.md`](02-mimari.md) | Katmanlar, sınır kuralları, istek akışı, iki ayrı frontend |
| 03 | [`03-depolama-ve-artefaktlar.md`](03-depolama-ve-artefaktlar.md) | Namespace izolasyonu, atomik yazma, artefakt kayıt defteri, geri alma |
| 04 | [`04-pipeline-fazlari.md`](04-pipeline-fazlari.md) | Faz -1'den yayına kadar tüm hat, işleme modları |
| 05 | [`05-onay-kapilari.md`](05-onay-kapilari.md) | SHA-256 bağlı onaylar, inceleme listeleri, yayın kapıları |
| 06 | [`06-is-kuyrugu-ve-otomasyon.md`](06-is-kuyrugu-ve-otomasyon.md) | Job modeli, lease, worker, progress, otomatik akış |
| 07 | [`07-saglayicilar-ve-router.md`](07-saglayicilar-ve-router.md) | Google router, kota defteri, çapraz sağlayıcı, sır yönetimi |
| 08 | [`08-render-ve-scene-editor.md`](08-render-ve-scene-editor.md) | FFmpeg filtergraph'ları, EDL modeli, Render Atölyesi |
| 09 | [`09-studyolar.md`](09-studyolar.md) | Turbo Studio (14 aşama), Long-form Studio (12 aşama), stage machine |
| 10 | [`10-api-referansi.md`](10-api-referansi.md) | Tüm HTTP uç noktaları, hata sözleşmesi |
| 11 | [`11-modul-referansi.md`](11-modul-referansi.md) | Dosya dosya sorumluluk tablosu |
| 12 | [`12-test-ve-dogrulama.md`](12-test-ve-dogrulama.md) | pytest, verify_*.py suiti, neyin mock olduğu |
| 13 | [`13-kurulum-ve-calistirma.md`](13-kurulum-ve-calistirma.md) | Kurulum, çalıştırma, portlar, bilinen tuzaklar |

## Kök dizindeki diğer belgeler

- [`../../README.md`](../../README.md) — Colab/notebook odaklı ana anlatım, Google router tabloları
- [`../../CONSOLE.md`](../../CONSOLE.md) — Creator Console kullanıcı kılavuzu (Türkçe)
- [`../../ROADMAP.md`](../../ROADMAP.md) — ürün fazları ve mevcut durum eşlemesi
- [`../../MILESTONES.md`](../../MILESTONES.md) — oturum oturum ilerleme, kabul kriterleri, kanıt
- [`../CLAUDE_RENDER_STUDIO_VIDEO_EDITOR_PLAN.md`](../CLAUDE_RENDER_STUDIO_VIDEO_EDITOR_PLAN.md) — video editör planı
- [`../M0_0_KARAR_PAKETI.md`](../M0_0_KARAR_PAKETI.md) — M0.0 karar kayıtları

## Bir cümlelik özet

Kullanıcının verdiği bir kaynak videodan, **her adımı insan onayına bağlı**,
yerel çalışan, sağlayıcı-bağımsız bir dikey Short (ve isteğe bağlı uzun form)
üretim hattı; hiçbir çıktı kendini onaylamaz ve hiçbir şey otomatik yayınlanmaz.
