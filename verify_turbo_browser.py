#!/usr/bin/env python
"""Can an operator actually drive Turbo Studio from the browser?

The Python suite proves the service and the routes work. That is not the same
question. This one clicks the real controls in real Chromium and checks that
the workflow advances -- because a route with no button is a feature nobody can
use, and that is exactly the gap this file was written to close.

Every provider is a fixture twin. No real Gemini, Pexels, Pixabay or TTS
request is made, and nothing is uploaded or published.
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from playwright.sync_api import sync_playwright

from creator_console import fixtures
from creator_console.app import ConsoleApp, ConsoleConfig
from creator_console.server import ConsoleServer
from local_pipeline import google_ai

CHECKS = 0
FAILURES = 0


def check(condition, label, message=""):
    global CHECKS, FAILURES
    CHECKS += 1
    if condition:
        print(f"  [{label}] OK" + (f" - {message}" if message else ""))
    else:
        FAILURES += 1
        print(f"  [{label}] BASARISIZ" + (f" - {message}" if message else ""))


def wav_bytes(seconds: float = 4.0) -> bytes:
    return b"\x00\x00" * int(google_ai.TTS_SAMPLE_RATE * seconds)


class FakeResponse:
    def __init__(self, payload=None, audio=None):
        self.text = payload
        self.usage_metadata = None
        if audio is not None:
            blob = type("Blob", (), {"data": audio, "mime_type": "audio/L16"})()
            part = type("Part", (), {"inline_data": blob, "text": None})()
        else:
            part = type("Part", (), {"inline_data": None, "text": payload})()
        content = type("Content", (), {"parts": [part]})()
        self.candidates = [type("Cand", (), {"content": content,
                                             "finish_reason": "STOP"})()]


class FakeGoogle:
    """Records requests; returns a usable script and real WAV bytes."""

    def __init__(self, beats=3):
        self.beats = beats
        self.text_calls = 0
        self.tts_calls = 0
        self.models = self

    def generate_content(self, *, model, contents, config=None, **_kwargs):
        if config and "AUDIO" in (config.get("response_modalities") or []):
            self.tts_calls += 1
            return FakeResponse(audio=wav_bytes(4.0))
        self.text_calls += 1
        sentences = [f"Bu {i}. sahnenin anlatim cumlesidir ve yeterince uzundur."
                     for i in range(1, self.beats + 1)]
        return FakeResponse(json.dumps({
            "hook": "Roma neden coktu?",
            "narration": " ".join(sentences),
            "scene_beats": [
                {"index": i + 1, "text": sentences[i],
                 "search_query": f"ancient roman scene {i + 1}",
                 "keywords": ["roma"]}
                for i in range(self.beats)],
        }, ensure_ascii=False))


def main() -> int:
    print("=" * 74)
    print("TURBO STUDIO TARAYICI DOGRULAMASI (gercek Chromium, sahte saglayici)")
    print("=" * 74)

    client = FakeGoogle(beats=3)
    with tempfile.TemporaryDirectory(prefix="turbo_browser_") as temp:
        root = Path(temp)
        app = ConsoleApp(ConsoleConfig(
            storage_root=root / "active", inline_jobs=True,
            secret_getter=fixtures.fake_secret_getter(),
            **{**fixtures.fixture_client_factories(),
               "google_client_factory": (lambda: client)}))
        pid = app.create_project(title="Turbo browser",
                                 created_by="test")["project_id"]
        server = ConsoleServer(app, host="127.0.0.1", port=0, quiet=True)
        server.serve_in_thread()
        base = f"http://127.0.0.1:{server.server_address[1]}"

        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch()
                page = browser.new_page(viewport={"width": 1280, "height": 1400})
                errors = []
                page.on("pageerror", lambda exc: errors.append(str(exc)))
                requests = []
                page.on("request", lambda r: requests.append((r.method, r.url)))

                print("\n--- 1. Sekme aciliyor ve hicbir sey harcamiyor ---")
                page.goto(f"{base}/?project={pid}#turbo", wait_until="networkidle")
                page.wait_for_selector("[data-testid=turbo-screen]", timeout=15000)
                check(page.locator("[data-testid=turbo-screen]").count() == 1,
                      "turbo-ekrani-aciliyor")
                check(page.locator("h1").filter(
                    has_text="Turbo Studio").count() == 1,
                    "kendi-basligi-var")
                check(client.text_calls == 0 and client.tts_calls == 0,
                      "sekme-acilisi-SIFIR-saglayici-cagrisi",
                      f"{client.text_calls} metin / {client.tts_calls} tts")
                writes = [(m, u) for m, u in requests
                          if m not in ("GET", "HEAD") and "/turbo" in u]
                check(not writes, "sekme-acilisi-yalnizca-OKUMA", str(writes[:2]))

                print("\n--- 2. Brief arayuzden kaydediliyor ---")
                page.fill("[data-testid=turbo-topic]",
                          "Roma imparatorlugunun cokus nedenleri")
                page.fill("[data-testid=turbo-duration]", "30")
                page.click("[data-testid=turbo-save-brief]")
                page.wait_for_timeout(1200)
                check(page.locator("[data-testid=turbo-estimates]").count() == 1,
                      "brief-kaydedildi-tahminler-gorunuyor")

                print("\n--- 3. Asama arayuzden calistiriliyor ---")
                run = page.locator("[data-testid=turbo-run-stage]")
                check(run.count() == 1, "asama-calistir-dugmesi-VAR")
                check(run.is_enabled(), "asama-calistir-dugmesi-ETKIN")
                run.click()
                page.wait_for_selector(
                    "[data-testid=turbo-stage-script] .badge--completed",
                    timeout=20000)
                check(True, "script-asamasi-arayuzden-CALISTI")
                check(client.text_calls == 1, "script-tek-cagri-yapti",
                      f"{client.text_calls} cagri")
                check(page.locator("[data-testid=turbo-script]").count() == 1,
                      "uretilen-script-ekranda-gorunuyor")

                print("\n--- 4. Insan kapisi arayuzden onaylaniyor ---")
                approve = page.locator("[data-testid=turbo-approve-script_review]")
                check(approve.count() == 1, "onay-dugmesi-VAR")
                approve.click()
                page.wait_for_selector(
                    "[data-testid=turbo-stage-script_review] .badge--completed",
                    timeout=15000)
                check(True, "insan-kapisi-arayuzden-ONAYLANDI")

                print("\n--- 5. Zincir devam ediyor (TTS + birlestirme) ---")
                page.click("[data-testid=turbo-run-stage]")
                page.wait_for_selector(
                    "[data-testid=turbo-stage-tts] .badge--completed",
                    timeout=60000)
                check(client.tts_calls >= 1, "TTS-arayuzden-calisti",
                      f"{client.tts_calls} parca")
                check(page.locator("[data-testid=turbo-narration]").count() == 1,
                      "anlatim-paneli-gorunuyor")

                print("\n--- 6. Kullanilamayan kontrol GEREKCESIYLE kapali ---")
                final_btn = page.locator("[data-testid=turbo-render-final-btn]")
                check(final_btn.count() == 1, "final-render-dugmesi-VAR")
                check(not final_btn.is_enabled(),
                      "onizleme-yokken-final-dugmesi-KAPALI")
                check("onizleme" in (final_btn.get_attribute("title") or "").lower(),
                      "kapali-dugme-GEREKCE-tasiyor",
                      final_btn.get_attribute("title") or "(bos)")

                print("\n--- 7. Muzik paneli telif iddiasi yapmiyor ---")
                music = page.locator("[data-testid=turbo-music]")
                check(music.count() == 1, "muzik-paneli-var")
                text = music.inner_text().lower()
                check("telifsiz olarak nitelemez" in text or "nitelemez" in text,
                      "muzik-paneli-telif-iddiasini-REDDEDIYOR")
                check(page.locator("[data-testid=turbo-music-none]").count() == 1,
                      "muziksiz-devam-secenegi-var")

                print("\n--- 8. Sayfa yenilenince durum korunuyor ---")
                page.reload(wait_until="networkidle")
                page.wait_for_selector(
                    "[data-testid=turbo-stage-script] .badge--completed",
                    timeout=15000)
                check(True, "yenilemeden-sonra-durum-KORUNDU")

                print("\n--- 9. JS hatasi ve sizinti yok ---")
                check(not errors, "JS-hatasi-YOK", str(errors[:2]))
                body = page.locator("body").inner_text()
                check("FAKE-CONSOLE" not in body and "GeminiKey" not in body,
                      "sayfada-kimlik-bilgisi-YOK")
                check(str(root) not in body, "sayfada-mutlak-yol-YOK")

                browser.close()
        finally:
            server.shutdown()

    print("\n" + "=" * 74)
    print(f"toplam {CHECKS} kontrol, {CHECKS - FAILURES} gecti, {FAILURES} basarisiz")
    if FAILURES:
        print("TURBO_BROWSER_BASARISIZ")
        return 1
    print("TURBO_BROWSER_OK (gercek Chromium; sahte saglayicilar, gercek API yok)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
