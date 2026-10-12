# 02 · Mimari

## Katmanlar

```
                    ┌──────────────────────────────────────┐
  Tarayıcı A        │ creator_console/static/  (app.js)    │  build-step yok,
  (üretim konsolu)  │ vanilla ES modülleri + CSS custom    │  Node gerekmez
                    └──────────────┬───────────────────────┘
                                   │ HTTP (JSON)
  Tarayıcı B        ┌──────────────┴───────────────────────┐
  (React iskeleti)  │ apps/web/  (React 19 + Vite)         │
                    └──────────────┬───────────────────────┘
                                   │
                    ┌──────────────┴───────────────────────┐
  Transport         │ creator_console/api.py  ConsoleApi    │  yalnızca taşıma
                    │   ↑                    ↑              │  (status, payload)
                    │ server.py          services/api/main │
                    │ (stdlib HTTP)      (FastAPI adaptör)  │
                    └──────────────┬───────────────────────┘
                                   │
  Uygulama çekirdeği│ creator_console/app.py  ConsoleApp    │  view model + iş yürütme
                    └──────────────┬───────────────────────┘
                                   │
  TEK giriş noktası │ creator_console/pipeline_service.py   │  4310 satır
                    └──────────────┬───────────────────────┘
                                   │
  Kanonik mantık    │ local_pipeline/*.py                   │  notebook'larla paylaşılır
                    └───────────────────────────────────────┘
```

## Sınır kuralları

1. **`pipeline_service.py`, konsolun `local_pipeline`'ı çağırdığı TEK yerdir.**
   Modülün kendi docstring'i bunu böyle tanımlar. `api.py` pipeline mantığı
   içermez, `app.py` view model üretir.
2. **Notebook mantığı frontend'e kopyalanmaz.** Tarayıcı sunucunun ürettiği view
   model'i render eder; `local_pipeline` adı bile bundle'da geçmez — bu bir
   testle doğrulanır.
3. **`ConsoleApi.handle()` taşımadan bağımsızdır**: `(status, payload)` tuple'ı
   döner. Aynı fonksiyon üç taşıyıcı tarafından sürülür:
   - `creator_console/server.py` — stdlib `ThreadingHTTPServer` (üretim konsolu)
   - `services/api/main.py` — FastAPI sarmalayıcı (React/API tüketicileri)
   - testler — doğrudan çağrı, HTTP olmadan
4. **Uzun işler `jobs.py` üzerinden yürür**; durum diskte tutulur, sayfa
   yenilemesi veya backend restart'ı işi kaybettirmez.
5. **Onaylar `local_pipeline.approval` SHA-256 kapılarından geçer**; UI'da onayı
   atlatacak yol yoktur.

## İstek akışı (örnek: EDL klip güncelleme)

```
PATCH /api/projects/prj_x/edl/clips/0
   │
   ├─ services/api/main.py:catch_all         → gövdeyi okur, ConsoleApi'ye devreder
   ├─ creator_console/api.py                 → yolu parse eder, alanları doğrular
   │                                            (_json_body, _required, x-actor)
   ├─ pipeline_service.update_edl_clip()     → EDL'i yükler, mutasyonu uygular
   │      ├─ scene_editor.set_clip_source_bounds()   (sınır doğrulama)
   │      ├─ scene_editor.crop_clip() / zoom_clip() / set_caption_style()
   │      ├─ write_artifact("edl", ...)      → atomik JSON yazımı
   │      └─ audit.record("edl.updated")     → kapalı sözlük kontrolü
   └─ JSONResponse (no-store, nosniff, no-referrer başlıkları)
```

FastAPI katmanı yanıt başlıklarını sabitler: `X-Content-Type-Options: nosniff`,
`Referrer-Policy: no-referrer`, `Cache-Control: no-store`. Medya için
`__raw__` / `__file__` özel yükleri `Response` / `FileResponse`'a çevrilir
(`Accept-Ranges: bytes` ile).

## İki frontend, iki amaç

| | `creator_console/static/` | `apps/web/` |
|---|---|---|
| Teknoloji | Vanilla ES modülleri, CSS custom property | React 19 + Vite 6 |
| Bağımlılık | **Yok** (yalnızca Python stdlib sunar) | npm (173 paket) |
| Port | 8765 (`python -m creator_console`) | 3000 (`npm run dev`) |
| Kapsam | Tüm üretim akışı (10 ekran) | Trend Radar · Sahne Editörü · Yayın · **Render Atölyesi** |
| Durum | Üretim yolu | İskelet / yeni editör deneyleri |
| API kökü | Aynı origin | `import.meta.env.VITE_API_ROOT ?? ''` → Vite proxy `/api` → `127.0.0.1:8000` |

React tarafı FastAPI'ye Vite proxy üzerinden gider
(`@apps/web/vite.config.js:6-14`). Backend'de
CORS middleware **yoktur**; bu yüzden mutlak `http://127.0.0.1:8000` kökü
kullanılmaz, göreli `/api` kullanılır.

## Çok kullanıcılıya hazırlık

Bugün tek kullanıcılı ve kimlik doğrulamasız olmasına rağmen taşıyıcı yapılar
gerçek:

| Alan | Durum |
|---|---|
| `user_id` / `workspace_id` / `project_id` / `job_id` | Gerçek, doğrulanmış, her artefaktta |
| Depolama namespace'i | `<root>/workspaces/<ws>/projects/<prj>/` — kaçış testli |
| Storage adapter | `StorageAdapter` arayüzü; nesne depolamaya geçiş call-site değiştirmez |
| Audit | Append-only JSONL, kapalı olay sözlüğü, sanitize |
| Sırlar | Sunucu tarafı referans; tarayıcıya yalnızca `configured`/`missing` |
| Job runner | `JobRunner` arayüzü; thread → harici worker geçişi API'yi bozmaz |
| Idempotency | `idempotency_key` + tek-claim lease |
