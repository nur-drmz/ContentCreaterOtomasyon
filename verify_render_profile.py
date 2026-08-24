#!/usr/bin/env python
"""Render Studio Hizli Revize -- Asama 1: sozlesme, resolver ve drift ayrimi.

    python verify_render_profile.py

NE KANITLIYOR
-------------
Render Studio bugune kadar SALT OKUNURDU. Operator cozunurlugu, kaliteyi,
altyazi temasini veya kadraji degistiremiyordu: `render_adjustment` yalnizca uc
kadraj degeri tutuyordu ve baska her override `check_config_drift` icinde hata
oluyordu -- yani bir caption temasi degistirmek Faz 0 planini yeniden
onaylatmayi gerektiriyordu.

Bu paket sunlari kanitlar:

  1. Profil YOKKEN render, planin dedigi gibi kalir. Varsayilanlar sabit kodlu
     degil, onaylanmis karardan okunur.
  2. Istemci ham encoder girdisi gonderemez. CRF, codec, font yolu diye bir alan
     yok; bilinmeyen alan sessizce dusurulmez, REDDEDILIR.
  3. Kalite preset'i SUNUCUDA width/height/crf/preset'e cozulur.
  4. Sunum anahtarlari (`render_mode`, `preview_mode`, `caption_style`,
     `caption_theme`) profille override edilebilir; altyazi ICERIGINI degistiren
     `subtitle_language` ve `subtitle_whisper_model_size` EDILEMEZ.
  5. Muafiyet yalnizca bu dort anahtar icin gecerlidir: listelenmemis yeni bir
     render ayari hala drift verir (fail-closed).
  6. Slot izolasyonu: bir slotun profili digerinin render'ini degistirmez.
  7. Ayni ayarlar = ayni hash. Zaman damgasi/aktor/gerekce hash'i degistirmez,
     yoksa her kayit hala gecerli bir render'i arsivlerdi.

NEGATIF KONTROL
---------------
Her davranis icin duzeltmeyi geri alan bir kontrol var: muafiyet bayragi
kapatilinca drift GERI GELMELI, allowlist gevsetilince deger GECMELI. Bir test
yalnizca kirik kodda dustugunu gosterdiginde yesil sayilir.

GUVENLIK
--------
Izole gecici depolama, sahte saglayicilar. Hicbir gercek API cagrilmaz, hicbir
video render edilmez.
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

from creator_console import fixtures, render_profile as rp        # noqa: E402
from creator_console.api import ConsoleApi                        # noqa: E402
from creator_console.app import ConsoleApp, ConsoleConfig         # noqa: E402
from creator_console.pipeline_service import PipelineServiceError  # noqa: E402
from creator_console.storage import atomic_write_json             # noqa: E402
from local_pipeline import render_utils, source_strategy          # noqa: E402

FAILURES: list = []
CHECKS = 0


def check(condition, label: str, message: str = "") -> bool:
    global CHECKS
    CHECKS += 1
    if condition:
        print(f"  [{label}] OK" + (f" - {message}" if message else ""))
        return True
    FAILURES.append(f"{label}: {message}")
    print(f"  [{label}] BASARISIZ - {message}")
    return False


def section(title: str) -> None:
    print(f"\n--- {title} ---")


def rejects(values, label, **kwargs) -> bool:
    """`validate` bu degeri reddetmeli ve gerekceyi operatore soylemeli."""
    try:
        rp.validate(values, **kwargs)
    except rp.RenderProfileError as exc:
        return check(bool(str(exc).strip()), label, str(exc)[:70])
    return check(False, label, "kabul edildi")



def call(api, method, path, body=None):
    raw = b"" if body is None else json.dumps(body).encode("utf-8")
    return api.handle(method, path, body=raw, headers={})


def seeded_project(app, *, outputs: int = 2):
    """A project with an approved-shape plan and fake render output per slot.

    Real renders are not needed to prove which FILES a save archives, and
    running FFmpeg per slot would make this suite minutes long for no extra
    evidence. The bytes are stand-ins; the PATHS are the real thing.
    """
    seeded = fixtures.seed_demo_project(app, title="Render profili")
    ownership = app.ownership(seeded["project_id"])
    service = app.service(ownership)
    service.create_processing_plan(
        mode="direct_clip_series", selected_by="verify", output_count=outputs,
        render_config={"render_mode": "blurred_background", "preview_mode": True,
                       "caption_style": "active_word_yellow",
                       "caption_theme": "auto"})
    return seeded["project_id"], ownership, service


def fake_render_output(app, ownership, service, slot: int) -> list:
    """Put a manifest + a video where a real render of `slot` would leave them."""
    written = []
    render_dir = service.slot_render_dir(slot)
    render_dir.mkdir(parents=True, exist_ok=True)
    video = render_dir / "final.mp4"
    video.write_bytes(b"not a real video, only a path stand-in")
    written.append(video)
    # `REQUIRED_FIELDS["render_manifest"]` is ("status", "output", "approval");
    # a manifest missing any of them is reported as a PROBLEM and never loaded,
    # so a stand-in has to carry them or it proves nothing.
    atomic_write_json(
        app.storage.resolve(ownership, service.artifact_relative("render_manifest", slot),
                            create_parent=True),
        {"artifact_type": "render_manifest", "schema_version": 1,
         "status": "review_required", "not_uploaded": True, "output_slot": slot,
         "output": {"relative_path": str(video.name), "validation": {}},
         "approval": {"status": "pending"}})
    atomic_write_json(
        app.storage.resolve(ownership, service.render_review_relative(slot),
                            create_parent=True),
        {"artifact_type": "render_review", "schema_version": 1, "items": []})
    return written

# --------------------------------------------------------------- 1. varsayilan
def test_defaults_follow_the_plan() -> None:
    section("1. Varsayilanlar PLANDAN okunur, sabit kodlu degil")

    plan = {"render": {"render_mode": "center_crop", "preview_mode": False,
                       "caption_theme": "game_of_thrones",
                       "caption_style": "active_word_yellow"}}
    profile = rp.default_profile(plan)
    check(profile["render_mode"] == "center_crop",
          "varsayilan-render-modu-PLANDAN", profile["render_mode"])
    check(profile["quality_preset"] == "final",
          "preview_mode=False-FINAL-kaliteye-cozulur", profile["quality_preset"])
    check(profile["caption_theme"] == "game_of_thrones",
          "varsayilan-tema-PLANDAN", profile["caption_theme"])

    # Negatif kontrol: farkli bir plan farkli bir varsayilan uretmeli. Ayni
    # ciktiyi verseydi degerler plandan degil sabitten geliyor olurdu.
    other = rp.default_profile({"render": {"render_mode": "blurred_background",
                                           "preview_mode": True}})
    check(other["render_mode"] == "blurred_background"
          and other["quality_preset"] == "preview",
          "NEGATIF-farkli-plan-farkli-varsayilan",
          f"{other['render_mode']}/{other['quality_preset']}")

    check(rp.default_profile(None)["render_mode"] in rp.RENDER_MODES,
          "plansiz-proje-de-gecerli-varsayilan-alir")

    # Profilsiz render plani AYNEN uygulamali.
    effective = rp.effective_render_config(plan, rp.default_profile(plan))
    planned = plan["render"]
    same = all(effective[k] == planned[k] for k in
               ("render_mode", "caption_theme", "caption_style"))
    check(same and effective["preview_mode"] is False,
          "PROFIL-YOKKEN-plan-aynen-uygulanir", str(effective))


# ------------------------------------------------------------- 2. allowlist
def test_allowlist_and_raw_input() -> None:
    section("2. Ham encoder girdisi ve bilinmeyen alan REDDEDILIR")

    rejects({"crf": 12}, "ham-CRF-REDDEDILIR")
    rejects({"encode_preset": "ultrafast"}, "ham-x264-preset-REDDEDILIR")
    rejects({"caption_font_path": "C:/evil.ttf"}, "font-YOLU-REDDEDILIR")
    rejects({"quality_preset": "ultra"}, "bilinmeyen-kalite-REDDEDILIR")
    rejects({"render_mode": "letterbox"}, "bilinmeyen-render-modu-REDDEDILIR")
    rejects({"caption_theme": "../../etc"}, "bilinmeyen-tema-REDDEDILIR")
    rejects({"resolution_preset": "8k"}, "bilinmeyen-cozunurluk-REDDEDILIR")
    rejects({"active_word_color": "#ff0000"}, "serbest-renk-REDDEDILIR")
    rejects({"frame_zoom": 9.0}, "aralik-disi-zoom-REDDEDILIR")
    rejects({"frame_focus_x": -0.2}, "negatif-odak-REDDEDILIR")
    rejects({"caption_font_size": 4}, "cok-kucuk-font-REDDEDILIR")
    rejects({"caption_font_size": 900}, "cok-buyuk-font-REDDEDILIR")
    rejects({"frame_zoom": "abc"}, "sayisal-olmayan-zoom-REDDEDILIR")

    # Negatif kontrol: allowlist'in ICINDEKI degerler gecmeli. Hepsi
    # reddedilseydi yukaridaki 13 kontrol hicbir sey kanitlamazdi.
    ok = rp.validate({"quality_preset": "draft", "render_mode": "center_crop",
                      "caption_theme": "game_of_thrones",
                      "active_word_color": "cyan", "frame_zoom": 1.5,
                      "caption_font_size": 64})
    check(ok["quality_preset"] == "draft" and ok["frame_zoom"] == 1.5,
          "NEGATIF-gecerli-degerler-KABUL-edilir", str(ok["caption_font_size"]))

    # Her tema gercekten yuklenebilir olmali: allowlist render_utils'ten gelir.
    themes = set(rp.allowed_themes())
    check(themes and themes <= set(render_utils.CAPTION_THEME_PROFILES),
          "tema-allowlist-RENDER_UTILS-ile-ayni", str(sorted(themes)))

    check(all(color.startswith("&H") and len(color) == 10
              for color in rp.ACTIVE_COLORS.values()),
          "renkler-gecerli-ASS-formatinda", str(sorted(rp.ACTIVE_COLORS)))


# ------------------------------------------------------------ 3. kalite cozumu
def test_quality_resolves_on_the_server() -> None:
    section("3. Kalite preset'i SUNUCUDA cozulur")

    final = rp.resolve_quality(rp.validate({"quality_preset": "final"}))
    check((final["render_width"], final["render_height"]) == (1080, 1920),
          "final-1080x1920", f"{final['render_width']}x{final['render_height']}")
    check(final["crf"] == 18 and final["encode_preset"] == "slow",
          "final-crf18-slow", f"crf={final['crf']}")
    check(final["is_delivery_quality"] is True, "final-TESLIM-kalitesi")

    preview = rp.resolve_quality(rp.validate({"quality_preset": "preview"}))
    check((preview["render_width"], preview["render_height"]) == (540, 960),
          "preview-yarim-olcek",
          f"{preview['render_width']}x{preview['render_height']}")
    check(preview["is_delivery_quality"] is False,
          "preview-TESLIM-KALITESI-DEGIL")

    draft = rp.resolve_quality(rp.validate({"quality_preset": "draft"}))
    check(draft["crf"] > preview["crf"] > final["crf"],
          "draft>preview>final-crf",
          f"{draft['crf']}/{preview['crf']}/{final['crf']}")
    check(draft["is_delivery_quality"] is False, "draft-TESLIM-KALITESI-DEGIL")

    # x264 yuv420p ile tek sayili boyut kabul etmez.
    for name in rp.QUALITY_PRESETS:
        for res in rp.RESOLUTION_PRESETS:
            got = rp.resolve_quality(rp.validate(
                {"quality_preset": name, "resolution_preset": res}))
            if got["render_width"] % 2 or got["render_height"] % 2:
                check(False, "her-preset-CIFT-boyut",
                      f"{name}/{res}: {got['render_width']}x{got['render_height']}")
                return
    check(True, "her-preset-CIFT-boyut",
          f"{len(rp.QUALITY_PRESETS) * len(rp.RESOLUTION_PRESETS)} kombinasyon")

    # Hedef cozunurluk olcekten bagimsiz kalir: preview kucuk render edilir ama
    # hala dikey Short'un hedefidir.
    check(preview["target_width"] == final["target_width"] == 1080,
          "hedef-cozunurluk-olcekten-BAGIMSIZ")


# ---------------------------------------------------------------- 4. drift
def test_drift_split() -> None:
    section("4. Sunum override edilebilir, altyazi ICERIGI edilemez")

    plan = {"decision": {"render": {
        "render_mode": "blurred_background", "preview_mode": True,
        "caption_style": "active_word_yellow", "caption_theme": "auto",
        "subtitle_language": "tr", "subtitle_whisper_model_size": "small"}}}

    presentation_changed = {**plan["decision"]["render"],
                            "render_mode": "center_crop",
                            "caption_theme": "game_of_thrones",
                            "preview_mode": False}

    errors = source_strategy.check_config_drift(
        plan, presentation_changed, presentation_overridden=True)
    check(not errors, "SUNUM-override-drift-VERMEZ", str(errors)[:90])

    # NEGATIF KONTROL: muafiyeti kaldir -- ayni konfigurasyon drift vermeli.
    # Vermeseydi yukaridaki kontrol muafiyeti degil, hicbir seyi test etmezdi.
    reverted = source_strategy.check_config_drift(plan, presentation_changed)
    check(len(reverted) == 3, "NEGATIF-muafiyetsiz-AYNI-config-drift-verir",
          f"{len(reverted)} hata")

    content_changed = {**plan["decision"]["render"], "subtitle_language": "en"}
    errors = source_strategy.check_config_drift(
        plan, content_changed, presentation_overridden=True)
    check(any("subtitle_language" in e for e in errors),
          "ALTYAZI-DILI-override-EDILEMEZ", str(errors)[:90])

    model_changed = {**plan["decision"]["render"],
                     "subtitle_whisper_model_size": "large-v3"}
    errors = source_strategy.check_config_drift(
        plan, model_changed, presentation_overridden=True)
    check(any("subtitle_whisper_model_size" in e for e in errors),
          "WHISPER-MODELI-override-EDILEMEZ", str(errors)[:90])

    # Fail-closed: listelenmemis yeni bir anahtar muafiyete RAGMEN drift verir.
    unknown_key = {**plan["decision"]["render"], "watermark_opacity": 0.9}
    errors = source_strategy.check_config_drift(
        plan, unknown_key, presentation_overridden=True)
    check(any("watermark_opacity" in e for e in errors),
          "LISTELENMEMIS-anahtar-hala-drift-verir-FAIL-CLOSED",
          str(errors)[:90])

    check(set(rp.OVERRIDABLE_RENDER_KEYS)
          == set(source_strategy.PRESENTATION_RENDER_KEYS),
          "iki-modulun-sunum-listesi-AYNI")
    check(not (set(source_strategy.PLAN_BOUND_RENDER_KEYS)
               & set(source_strategy.PRESENTATION_RENDER_KEYS)),
          "plan-bagli-ve-sunum-listeleri-KESISMIYOR")

    # Plan-bagli anahtarlar profile alanlarinda HIC yer almamali.
    check(not (set(source_strategy.PLAN_BOUND_RENDER_KEYS)
               & set(rp.default_profile())),
          "plan-bagli-anahtar-profilde-ALAN-DEGIL")

    # Resolver plan-bagli anahtarlari degistirmeden gecirir.
    effective = rp.effective_render_config(
        plan["decision"], rp.validate({"render_mode": "center_crop"}))
    check(effective["subtitle_language"] == "tr"
          and effective["subtitle_whisper_model_size"] == "small",
          "resolver-altyazi-anahtarlarini-DEGISTIRMEDEN-gecirir")


# ------------------------------------------------------------------ 5. hash
def test_content_hash() -> None:
    section("5. Ayni ayarlar = ayni hash")

    profile = rp.validate({"quality_preset": "final"})
    first = rp.build_artifact(profile, slot=1, base_plan_hash="abc",
                              actor="a", reason="ilk")
    second = rp.build_artifact(profile, slot=1, base_plan_hash="abc",
                               actor="baska-kisi", reason="tamamen baska gerekce")
    check(first["content_sha256"] == second["content_sha256"],
          "aktor-ve-gerekce-hash'i-DEGISTIRMEZ", first["content_sha256"][:12])
    check(first["updated_at"] != second["updated_at"]
          or first["updated_by"] != second["updated_by"],
          "ama-artefakt-yine-de-kimin-yazdigini-tutuyor")

    # NEGATIF KONTROL: bir AYAR degisince hash degismeli.
    changed = rp.validate({"quality_preset": "draft"})
    check(rp.content_sha256(changed) != first["content_sha256"],
          "NEGATIF-ayar-degisince-hash-DEGISIR")

    check(rp.changed_fields(profile, changed) == ["quality_preset"],
          "changed_fields-yalnizca-DEGISEN-alani-sayar",
          str(rp.changed_fields(profile, changed)))
    check(rp.changed_fields(profile, profile) == [],
          "ayni-profil-DEGISIKLIK-YOK")

    # Gerekce operator metnidir; alan ADLARI raporlanir, degerleri degil.
    noisy = rp.validate({"quality_preset": "draft", "reason": "gizli not"})
    check("reason" not in rp.changed_fields(profile, noisy),
          "serbest-metin-degisiklik-listesine-GIRMEZ")


# ---------------------------------------------------------- 6. slot izolasyonu
def test_slot_isolation_and_migration() -> None:
    section("6. Slot izolasyonu ve render_adjustment migrasyonu")

    root = Path(tempfile.mkdtemp(prefix="render_profile_"))
    try:
        app = ConsoleApp(ConsoleConfig(
            storage_root=root, inline_jobs=True,
            secret_getter=fixtures.fake_secret_getter(),
            **fixtures.fixture_client_factories()))
        seeded = fixtures.seed_demo_project(app, title="Render profili")
        ownership = app.ownership(seeded["project_id"])
        service = app.service(ownership)
        # Slot izolasyonu ancak plan birden fazla cikti tanimladiginda anlamli;
        # `require_slot` planda olmayan bir slotu zaten reddediyor.
        service.create_processing_plan(
            mode="direct_clip_series", selected_by="verify", output_count=2,
            render_config={"render_mode": "blurred_background",
                           "preview_mode": True,
                           "caption_style": "active_word_yellow",
                           "caption_theme": "auto"})

        one = service.render_profile_relative(1)
        two = service.render_profile_relative(2)
        check(one != two and one.endswith("output_01.json")
              and two.endswith("output_02.json"),
              "her-slotun-AYRI-dosyasi", f"{one} | {two}")

        check(service.render_profile(1)["source"] == "plan_default",
              "profilsiz-slot-PLANDAN-okur",
              service.render_profile(1)["source"])

        # Eski kadraj artefakti bir kez profile tasinir.
        service.save_render_adjustment(
            {"frame_zoom": 1.2, "frame_focus_x": 0.3, "frame_focus_y": 0.5},
            notes="sola kaydir", actor="verify", slot=1)
        migrated = service.render_profile(1)
        check(migrated["source"] == "migrated_render_adjustment",
              "eski-kadraj-artefakti-TASINDI", migrated["source"])
        check(migrated["settings"]["frame_zoom"] == 1.2
              and migrated["settings"]["frame_focus_x"] == 0.3,
              "kadraj-degerleri-KORUNDU", str(migrated["settings"]["frame_zoom"]))

        # NEGATIF KONTROL: slot 2 dokunulmamis olmali. Migrasyon slot'a
        # bakmasaydi bu deger 1.2 gelirdi.
        check(service.render_profile(2)["settings"]["frame_zoom"] == 1.0,
              "NEGATIF-slot-2-ETKILENMEDI",
              str(service.render_profile(2)["settings"]["frame_zoom"]))

        # Kaydedilmis bir profil, tasinan kadraj artefaktini gecersiz kilar.
        saved = rp.validate({"quality_preset": "draft", "frame_zoom": 1.8,
                             "render_mode": "center_crop"})
        atomic_write_json(
            app.storage.resolve(ownership, service.render_profile_relative(1),
                                create_parent=True),
            rp.build_artifact(saved, slot=1, base_plan_hash=None, actor="verify"))
        loaded = service.render_profile(1)
        check(loaded["source"] == "saved_profile"
              and loaded["settings"]["frame_zoom"] == 1.8,
              "kaydedilmis-profil-eski-artefakti-EZER",
              f"{loaded['source']}/{loaded['settings']['frame_zoom']}")

        effective = service.effective_render_config(1)
        check(effective["presentation_overridden"] is True,
              "degistirilmis-profil-OVERRIDE-olarak-isaretlenir")
        check(effective["config"]["render_mode"] == "center_crop",
              "effective-config-profildeki-modu-KULLANIR",
              effective["config"]["render_mode"])

        # Bayrak "profil degisti" demez, "drift'in baktigi bir anahtar degisti"
        # der. `draft` ve `preview` ikisi de `preview_mode=True`'ya cozulur, yani
        # yalnizca kaliteyi dusurmek drift muafiyeti ACMAMALI -- acsaydi bayrak
        # hicbir sey degismeden korumayi devre disi birakirdi.
        quality_only = rp.effective_render_config(
            service.plan_decision(), rp.validate({"quality_preset": "draft"}))
        plan_only = rp.effective_render_config(
            service.plan_decision(),
            rp.default_profile(service.plan_decision()))
        check(quality_only == plan_only,
              "yalnizca-kalite-degisimi-drift-anahtarina-DOKUNMAZ",
              str(quality_only["preview_mode"]))
        check(effective["quality"]["is_delivery_quality"] is False,
              "draft-profil-TESLIM-kalitesi-degil")
        check(effective["framing"]["frame_zoom"] == 1.8,
              "effective-config-kadraji-TASIYOR")

        # NEGATIF KONTROL: dokunulmamis slot 2 override sayilMAmali, yoksa
        # bayrak drift kontrolunu hic kimse duzenlemeden devre disi birakirdi.
        check(service.effective_render_config(2)["presentation_overridden"] is False,
              "NEGATIF-dokunulmamis-slot-OVERRIDE-DEGIL")

        # Bozuk profil render ekranini kilitlememeli.
        atomic_write_json(
            app.storage.resolve(ownership, service.render_profile_relative(1)),
            {"artifact_type": "render_profile", "schema_version": 99,
             "settings": {"quality_preset": "gelecekten-bir-deger"}})
        broken = service.render_profile(1)
        check(broken["source"] == "plan_default_invalid_profile",
              "GECERSIZ-profil-plana-duser-COKMEZ", broken["source"])
        check(broken["settings"]["quality_preset"] in rp.QUALITY_PRESETS,
              "dusulen-deger-yine-de-GECERLI")
    finally:
        shutil.rmtree(root, ignore_errors=True)




# ------------------------------------------------- 7. kaydetme ve arsivleme
def test_save_archives_only_this_slot() -> None:
    section("7. Kaydetme: yalnizca BU slotun render'i arsivlenir")

    root = Path(tempfile.mkdtemp(prefix="render_profile_save_"))
    try:
        app = ConsoleApp(ConsoleConfig(
            storage_root=root, inline_jobs=True,
            secret_getter=fixtures.fake_secret_getter(),
            **fixtures.fixture_client_factories()))
        _pid, ownership, service = seeded_project(app, outputs=2)

        slot1_files = fake_render_output(app, ownership, service, 1)
        slot2_files = fake_render_output(app, ownership, service, 2)
        check(all(f.exists() for f in slot1_files + slot2_files),
              "iki-slotta-da-sahte-render-var")

        impact = service.render_profile_impact(1)
        paths = {entry["path"] for entry in impact["moves"]}
        check(not any("output_02" in path for path in paths),
              "slot-1-etki-plani-slot-2-yi-ICERMIYOR", str(sorted(paths)))
        check(impact["file_count"] > 0, "slot-1-arsivlenecek-dosya-var",
              str(impact["file_count"]))

        result = service.save_render_profile(
            {"caption_theme": "game_of_thrones"}, actor="verify", slot=1)
        check(result["changed_fields"] == ["caption_theme"],
              "degisen-alan-DOGRU-raporlanir", str(result["changed_fields"]))
        check(result["render_required"] is True, "kayit-RENDER-GEREKLI-diyor")
        check(result["archived"] is not None, "eski-render-ARSIVLENDI")

        check(not any(f.exists() for f in slot1_files),
              "slot-1-render-dosyalari-TASINDI")
        # ASIL KONTROL: baska bir Short'un onayli videosu yerinde durmali.
        check(all(f.exists() for f in slot2_files),
              "SLOT-2-RENDER-DOKUNULMADI",
              str([f.name for f in slot2_files if f.exists()]))

        moved = result["archived"]["moved_paths"]
        check(moved and not any("output_02" in path for path in moved),
              "arsiv-manifestinde-slot-2-YOK", str(moved))

        # Script/ses/moment korunmali: gorsel bir revizyon saglayici parasi
        # harcamamali.
        for key in ("selected_script", "moment_selection"):
            relative = service.artifact_relative(key, 1)
            check(not any(relative in path for path in moved),
                  f"{key}-KORUNDU")

        # Slot 2'nin moment secimi ve transkripti slot KLASORUNUN ICINDE yasar
        # (slot 1'inkiler ust seviyede). Klasoru topluca arsivlemek, "caption
        # temasini degistir" islemini "momentini kaybet"e cevirir.
        selection = app.storage.resolve(
            ownership, service.artifact_relative("moment_selection", 2),
            create_parent=True)
        atomic_write_json(selection, {"artifact_type": "moment_selection",
                                      "status": "ok", "selected_moment_rank": 1})
        transcript = app.storage.resolve(
            ownership, service.artifact_relative("moment_transcript", 2),
            create_parent=True)
        atomic_write_json(transcript, {"artifact_type": "moment_transcript",
                                       "status": "ok", "words": [], "cues": []})

        # NEGATIF KONTROL: slot 2'nin kendi kaydi slot 2'yi arsivlemeli. Aksi
        # halde yukaridaki "dokunulmadi" kontrolu, arsivlemenin hic
        # calismamasindan da gecerdi.
        result2 = service.save_render_profile(
            {"caption_theme": "game_of_thrones"}, actor="verify", slot=2)
        check(result2["archived"] is not None and not any(f.exists() for f in slot2_files),
              "NEGATIF-slot-2-kendi-kaydinda-ARSIVLENIR")
        check(selection.exists(),
              "SLOT-2-MOMENT-SECIMI-korundu",
              "gorsel revizyon momenti yok etmemeli")
        check(transcript.exists(),
              "SLOT-2-TRANSKRIPTI-korundu",
              "gorsel revizyon onayli altyazi metnini yok etmemeli")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_stale_and_idempotent_saves() -> None:
    section("8. Bayat yazma reddi ve gereksiz arsivleme yok")

    root = Path(tempfile.mkdtemp(prefix="render_profile_stale_"))
    try:
        app = ConsoleApp(ConsoleConfig(
            storage_root=root, inline_jobs=True,
            secret_getter=fixtures.fake_secret_getter(),
            **fixtures.fixture_client_factories()))
        pid, ownership, service = seeded_project(app, outputs=1)
        api = ConsoleApi(app)

        first = service.save_render_profile(
            {"quality_preset": "draft"}, actor="verify",
            expected_hash=service.render_profile(1)["content_sha256"], slot=1)
        check(first["unchanged"] is False, "ilk-kayit-DEGISIKLIK-sayilir")

        # Ayni ayarlar tekrar: hicbir sey arsivlenmemeli.
        files = fake_render_output(app, ownership, service, 1)
        again = service.save_render_profile(
            {"quality_preset": "draft"}, actor="verify", slot=1)
        check(again["unchanged"] is True and again["archived"] is None,
              "AYNI-ayarlar-hicbir-sey-ARSIVLEMEZ")
        check(all(f.exists() for f in files),
              "gereksiz-kayitta-render-YERINDE-kaldi")
        check(again["render_required"] is False,
              "degismeyen-kayit-YENIDEN-RENDER-istemez")

        # NEGATIF KONTROL: gercek bir degisiklik yine arsivlemeli.
        changed = service.save_render_profile(
            {"quality_preset": "final"}, actor="verify", slot=1)
        check(changed["archived"] is not None and not files[0].exists(),
              "NEGATIF-gercek-degisiklik-ARSIVLER")

        # Bayat hash reddi.
        try:
            service.save_render_profile({"quality_preset": "draft"},
                                        actor="verify", expected_hash="eski-hash",
                                        slot=1)
            check(False, "BAYAT-hash-REDDEDILIR", "kabul edildi")
        except PipelineServiceError as exc:
            check("degisti" in str(exc), "BAYAT-hash-REDDEDILIR", str(exc)[:60])

        # NEGATIF KONTROL: dogru hash gecmeli.
        fresh = service.render_profile(1)["content_sha256"]
        ok = service.save_render_profile({"quality_preset": "preview"},
                                         actor="verify", expected_hash=fresh, slot=1)
        check(ok["unchanged"] is False, "NEGATIF-guncel-hash-KABUL-edilir")

        # Reddedilen kayit hicbir sey yazmamali.
        before = service.render_profile(1)["content_sha256"]
        try:
            service.save_render_profile({"quality_preset": "ultra"},
                                        actor="verify", slot=1)
        except PipelineServiceError:
            pass
        check(service.render_profile(1)["content_sha256"] == before,
              "REDDEDILEN-kayit-artefakti-DEGISTIRMEDI")

        # API yuzeyi
        status, payload = call(api, "GET", f"/api/projects/{pid}/render-profile?slot=1")
        check(status == 200 and "render_profile" in payload,
              "GET-render-profile-200", str(status))
        options = payload["render_profile"]["options"]
        check(set(options["quality_presets"]) == set(rp.QUALITY_PRESETS),
              "API-kalite-secenekleri-SUNUCUDAN", str(options["quality_presets"]))
        check("crf" not in json.dumps(options),
              "API-secenekleri-ham-CRF-SUNMUYOR")

        status, payload = call(api, "POST", f"/api/projects/{pid}/render-profile",
                               {"action": "save", "slot": 1,
                                "settings": {"quality_preset": "ultra"}})
        check(status >= 400, "API-gecersiz-deger-4xx", str(status))

        status, payload = call(api, "POST", f"/api/projects/{pid}/render-profile",
                               {"action": "save", "slot": 1,
                                "expected_hash": "bayat",
                                "settings": {"quality_preset": "draft"}})
        check(status >= 400, "API-bayat-hash-4xx", str(status))

        status, payload = call(api, "POST", f"/api/projects/{pid}/render-profile",
                               {"action": "reset", "slot": 1})
        check(status == 200, "API-Faz-0-varsayilanina-DONUS-200", str(status))
        after = service.render_profile(1)["settings"]
        check(after == rp.default_profile(service.plan_decision(), slot=1),
              "reset-PLAN-varsayilanini-geri-getirdi")

        # Denetim izinde serbest metin olmamali. Gerekce GERCEKTEN kaydedilir,
        # yoksa asagidaki kontrol hicbir sey kanitlamaz.
        service.save_render_profile({"render_mode": "center_crop"}, actor="verify",
                                    reason="gizli-gerekce-metni", slot=1)
        trail = "\n".join(json.dumps(e) for e in service.audit.events(limit=200))
        check("render_profile.saved" in trail, "audit-kaydi-YAZILDI")
        check("render_mode" in trail, "audit-DEGISEN-ALAN-adini-tasiyor")
        check("gizli-gerekce-metni" not in trail,
              "audit-serbest-metin-TASIMIYOR")
        # Gerekce yine de artefaktta duruyor: operator neden degistirdigini
        # gorebilmeli, sadece denetim izi metni tasimamali.
        saved = service.load_artifact_path(service.render_profile_relative(1))
        check(saved.get("reason") == "gizli-gerekce-metni",
              "gerekce-ARTEFAKTTA-korunuyor")
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------- 9. panel gorunumu
def test_profile_view() -> None:
    section("9. Panel gorunumu sunucuda karar verilir")

    root = Path(tempfile.mkdtemp(prefix="render_profile_view_"))
    try:
        app = ConsoleApp(ConsoleConfig(
            storage_root=root, inline_jobs=True,
            secret_getter=fixtures.fake_secret_getter(),
            **fixtures.fixture_client_factories()))
        _pid, ownership, service = seeded_project(app, outputs=2)

        view = service.render_profile_view(1)
        check(view["available"] is True, "panel-ACIK")
        check(view["has_output"] is False, "cikti-yokken-HAS_OUTPUT-false")
        check(view["rendered_settings"] is None,
              "cikti-yokken-KUNYE-yok")

        # Secenekler sunucudan gelmeli; tarayici allowlist'i yeniden yazmamali.
        options = view["options"]
        check(set(options["quality_presets"]) == set(rp.QUALITY_PRESETS),
              "panel-kalite-secenekleri-SUNUCUDAN")
        check(options["caption_themes"][0] == "auto"
              and set(options["caption_themes"][1:]) == set(rp.allowed_themes()),
              "panel-tema-secenekleri-ALLOWLIST-ile-ayni")
        check(options["font_size_range"] == [rp.FONT_SIZE_MIN, rp.FONT_SIZE_MAX],
              "panel-font-araligi-SUNUCUDAN", str(options["font_size_range"]))
        check("crf" not in json.dumps(options) and "preset\":" not in json.dumps(options),
              "panel-ham-encoder-degeri-SUNMUYOR")

        # Damgasiz eski bir render "bilinmiyor" demeli, uydurmamali.
        fake_render_output(app, ownership, service, 1)
        view = service.render_profile_view(1)
        check(view["has_output"] is True, "cikti-varken-HAS_OUTPUT-true")
        check(view["unknown_rendered_settings"] is True,
              "DAMGASIZ-eski-render-BILINMIYOR-diyor")
        check(view["rendered_quality_preset"] is None,
              "damgasiz-render-kalite-UYDURMUYOR")

        # NEGATIF KONTROL: damgali bir manifest kunyeyi okumali. Okumasaydi
        # yukaridaki "bilinmiyor" kontrolu, alanin hic okunmamasindan da gecerdi.
        settings = rp.validate({"quality_preset": "draft"})
        atomic_write_json(
            app.storage.resolve(ownership,
                                service.artifact_relative("render_manifest", 1)),
            {"artifact_type": "render_manifest", "schema_version": 1,
             "status": "review_required", "not_uploaded": True, "output_slot": 1,
             "output": {"relative_path": "final.mp4", "validation": {}},
             "approval": {"status": "pending"},
             "render_profile": {"content_sha256": rp.content_sha256(settings),
                                "settings": settings,
                                "quality_preset": "draft",
                                "is_delivery_quality": False}})
        view = service.render_profile_view(1)
        check(view["unknown_rendered_settings"] is False,
              "NEGATIF-damgali-render-BILINIYOR")
        check(view["rendered_quality_preset"] == "draft",
              "kunye-KALITEYI-tasiyor", str(view["rendered_quality_preset"]))
        check(view["rendered_is_delivery_quality"] is False,
              "kunye-TESLIM-KALITESI-DEGIL-diyor")

        # Slot 2 kendi gorunumunu almali.
        check(service.render_profile_view(2)["has_output"] is False,
              "slot-2-panel-AYRI")

        # Planda olmayan slot panelde de yok.
        check(service.render_profile_view(9)["available"] is False,
              "PLANDA-OLMAYAN-slot-panel-ACMAZ")
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ------------------------------------------- 10. profil GERCEKTEN render'a gidiyor
def test_profile_reaches_the_encoder() -> None:
    """Kaydedilen bir ayarin encoder'a ULASTIGINI kanitla.

    Bu paketin geri kalani artefakti dogruluyor: dogru dosya, dogru hash, dogru
    arsiv. Ama bir ayar kaydedilip render'a hic gecmeseydi bunlarin hepsi yine
    yesil olurdu -- ve operator caption temasini degistirip ayni videoyu geri
    alirdi. Burada render pipeline'i bir CASUS ile degistirilip `_run_render`'in
    ne gonderdigine bakiliyor; ffmpeg calistirilmiyor.
    """
    section("10. Kaydedilen profil ffmpeg cagrisina ULASIYOR")

    root = Path(tempfile.mkdtemp(prefix="render_profile_wiring_"))
    try:
        app = ConsoleApp(ConsoleConfig(
            storage_root=root, inline_jobs=True,
            secret_getter=fixtures.fake_secret_getter(),
            **fixtures.fixture_client_factories()))
        _pid, ownership, service = seeded_project(app, outputs=1)

        # Render'in ihtiyac duydugu yukari akis artefaktlari (sentetik).
        atomic_write_json(
            app.storage.resolve(ownership, "results/hot_moments.json",
                                create_parent=True),
            {"status": "ok", "hot_moments": [
                {"rank": 1, "source_start_sec": 1.0, "source_end_sec": 4.0,
                 "aligned_start_sec": 1.0, "aligned_end_sec": 4.0}],
             "source": {"duration_sec": 6.0,
                        "raw_video_path": str(service.source_path())}})
        atomic_write_json(
            app.storage.resolve(ownership,
                                service.artifact_relative("moment_selection", 1),
                                create_parent=True),
            {"artifact_type": "moment_selection", "status": "ok",
             "selected_moment_rank": 1})
        atomic_write_json(
            app.storage.resolve(ownership,
                                service.artifact_relative("moment_transcript", 1),
                                create_parent=True),
            {"artifact_type": "moment_transcript", "status": "ok",
             # Bos bir transkript ONAYLANAMAZ (kapi "onaylanacak metin yok"
             # diyor ve haklidir), bu yuzden fixture gercek kelime tasiyor.
             "words": [{"word": "Sessiz", "start": 1.0, "end": 1.5,
                        "probability": 0.9},
                       {"word": "bir", "start": 1.5, "end": 1.8,
                        "probability": 0.9},
                       {"word": "sahne", "start": 1.8, "end": 2.4,
                        "probability": 0.9}],
             "cues": [{"index": 1, "start": 1.0, "end": 2.4,
                       "text": "Sessiz bir sahne"}]})

        # Transkript kapisi gercek: onaysiz metinle render acilmaz. Burada bir
        # operator adina onaylaniyor -- kapinin GERCEKTEN kapali oldugu
        # verify_transcript_editor'de kanitlaniyor, burada degil.
        state = service.approval_state("transcript", 1)
        service.approve("transcript", reviewed_sha256=state["content_sha256"],
                        approved_by="verify")

        service.save_render_profile(
            {"quality_preset": "draft", "resolution_preset": "vertical_720x1280",
             "render_mode": "center_crop", "caption_theme": "game_of_thrones",
             "active_word_highlight": False, "frame_zoom": 1.4,
             "caption_font_size": 72},
            actor="verify", slot=1)

        captured = {}

        def spy(*args, **kwargs):
            captured.update(kwargs)
            # `_run_render` bundan sonra manifesti yazip dogruluyor; hicbir sey
            # render etmeden gecerli bir sekil dondur.
            return {"status": "failed", "status_reason": "casus (render yok)",
                    "output": {}, "approval": {"status": "pending"}}

        class _Tracker:
            def note(self, *a, **k): pass
            def fail(self, *a, **k): pass
            def finish(self, *a, **k): pass
            def __getattr__(self, name): return lambda *a, **k: None

        original = render_utils.run_original_audio_subtitle_pipeline
        render_utils.run_original_audio_subtitle_pipeline = spy
        try:
            service._run_render(
                _Tracker(), render_mode="blurred_background", preview_mode=True,
                subtitle_whisper_model_size="small", subtitle_language="auto",
                caption_style="active_word_yellow", caption_theme="auto",
                job_id="job_spy", actor="verify", slot=1)
        except Exception as exc:                       # noqa: BLE001
            check(False, "casus-render-cagrildi", f"{type(exc).__name__}: {exc}")
            return
        finally:
            render_utils.run_original_audio_subtitle_pipeline = original

        check(captured.get("render_mode") == "center_crop",
              "PROFILDEKI-render-modu-ffmpeg'e-gitti",
              str(captured.get("render_mode")))
        check(captured.get("caption_theme") == "game_of_thrones",
              "PROFILDEKI-tema-ffmpeg'e-gitti", str(captured.get("caption_theme")))
        check(captured.get("caption_style") != "active_word_yellow",
              "vurgu-KAPALI-oldugunda-dinamik-altyazi-istenmiyor",
              str(captured.get("caption_style")))
        check(captured.get("caption_font_size") == 72,
              "PROFILDEKI-font-boyutu-ffmpeg'e-gitti",
              str(captured.get("caption_font_size")))
        check((captured.get("target_width"), captured.get("target_height"))
              == (720, 1280),
              "PROFILDEKI-cozunurluk-ffmpeg'e-gitti",
              f"{captured.get('target_width')}x{captured.get('target_height')}")
        check((captured.get("encode_profile") or {}).get("crf") == 32,
              "DRAFT-preset-crf'i-ffmpeg'e-gitti",
              str((captured.get("encode_profile") or {}).get("crf")))
        check(captured.get("frame_zoom") == 1.4,
              "PROFILDEKI-kadraj-ffmpeg'e-gitti", str(captured.get("frame_zoom")))

        # Cagirana verilen argumanlar profil tarafindan EZILMELI: aksi halde
        # ayar kaydedilir ama eski deger render edilirdi.
        check(captured.get("render_mode") != "blurred_background",
              "NEGATIF-cagiranin-eski-argumani-EZILDI")
        # Plan-bagli anahtarlar ise cagirandan AYNEN gecmeli.
        check(captured.get("subtitle_language") == "auto"
              and captured.get("subtitle_whisper_model_size") == "small",
              "plan-bagli-anahtarlar-DEGISTIRILMEDEN-gecti")

        # Manifest hangi profille uretildigini damgalamis olmali.
        manifest = service.load_artifact("render_manifest", slot=1)["data"] or {}
        stamp = manifest.get("render_profile") or {}
        check(stamp.get("quality_preset") == "draft",
              "manifest-KUNYESI-damgalandi", str(stamp.get("quality_preset")))
        check(stamp.get("is_delivery_quality") is False,
              "kunye-teslim-kalitesi-OLMADIGINI-soyluyor")

        # Duzenlenmemisken override GONDERILMEMELI: gonderilseydi her render
        # "elle duzenlendi" damgasi tasir ve hicbir sey degistirmeden
        # `resolve_render_source`'un kendi mantigini devre disi birakirdi.
        check(captured.get("source_bounds_override") is None,
              "duzenleme-YOKKEN-override-gonderilmiyor",
              str(captured.get("source_bounds_override")))

        # Ayni casusla klip araligi: kaydedilen aralik ffmpeg'e ULASMALI.
        service.save_clip_bounds(start=0.5, end=3.0, actor="verify", slot=1)
        # Direct modda aralik degisimi transkript onayini dusurur -- render
        # yeniden acilsin diye yeni araligin metni tekrar onaylanir.
        atomic_write_json(
            app.storage.resolve(ownership,
                                service.artifact_relative("moment_transcript", 1),
                                create_parent=True),
            {"artifact_type": "moment_transcript", "status": "ok",
             "words": [{"word": "Yeni", "start": 0.5, "end": 1.0,
                        "probability": 0.9}],
             "cues": [{"index": 1, "start": 0.5, "end": 1.0, "text": "Yeni"}]})
        state = service.approval_state("transcript", 1)
        service.approve("transcript", reviewed_sha256=state["content_sha256"],
                        approved_by="verify")

        captured.clear()
        render_utils.run_original_audio_subtitle_pipeline = spy
        try:
            service._run_render(
                _Tracker(), render_mode="blurred_background", preview_mode=True,
                subtitle_whisper_model_size="small", subtitle_language="auto",
                caption_style="active_word_yellow", caption_theme="auto",
                job_id="job_spy2", actor="verify", slot=1)
        finally:
            render_utils.run_original_audio_subtitle_pipeline = original

        check(captured.get("source_bounds_override")
              == {"source_start_sec": 0.5, "source_end_sec": 3.0},
              "KAYDEDILEN-klip-araligi-ffmpeg'e-gitti",
              str(captured.get("source_bounds_override")))
    finally:
        shutil.rmtree(root, ignore_errors=True)


def main() -> int:
    print("=" * 74)
    print("RENDER PROFILI DOGRULAMA -- Asama 1-3 (sahte saglayicilar)")
    print("=" * 74)
    test_defaults_follow_the_plan()
    test_allowlist_and_raw_input()
    test_quality_resolves_on_the_server()
    test_drift_split()
    test_content_hash()
    test_slot_isolation_and_migration()
    test_save_archives_only_this_slot()
    test_stale_and_idempotent_saves()
    test_profile_view()
    test_profile_reaches_the_encoder()

    print("\n" + "=" * 74)
    print(f"toplam {CHECKS} kontrol, {CHECKS - len(FAILURES)} gecti, "
          f"{len(FAILURES)} basarisiz")
    if FAILURES:
        for line in FAILURES:
            print("  BASARISIZ:", line)
        print("RENDER_PROFILE_BASARISIZ")
        return 1
    print("RENDER_PROFILE_OK (izole depolama, sahte saglayicilar; render "
          "calistirilmadi, gercek API cagrilmadi)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
