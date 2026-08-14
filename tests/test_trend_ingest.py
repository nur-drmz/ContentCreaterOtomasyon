"""Trend ingest + rights gate. Nothing here touches the network."""
from __future__ import annotations

import pytest

from local_pipeline import trend_ingest


DISCOVERY = {
    "status": "review_required",
    "videos": [
        {
            "video_id": "abc123XYZ_9", "title": "Trend Video", "channel_id": "UC_a",
            "channel_title": "Ornek Kanal", "duration_sec": 620, "view_count": 900_000,
            "trend_score": 91.2, "views_per_hour": 5000.0, "matched_terms": ["ornek"],
            "discovery_rank": 1, "published_at": "2026-08-10T00:00:00Z",
        },
        {
            "video_id": "def456", "title": "Ikinci", "channel_id": "UC_b",
            "channel_title": "Baska Kanal", "duration_sec": 120, "trend_score": 70.0,
            "discovery_rank": 2,
        },
    ],
}


class TestSourceCandidates:
    def test_discovery_becomes_suggestions_and_nothing_is_selected(self):
        artifact = trend_ingest.build_source_candidates(DISCOVERY)
        assert artifact["status"] == "review_required"
        assert artifact["candidate_count"] == 2
        assert artifact["auto_selected"] is False
        assert artifact["selected_candidate_url"] is None
        assert artifact["requires_rights_decision"] is True
        assert all(row["rights_decision_required"] for row in artifact["candidates"])

    def test_manual_urls_are_merged_and_invalid_ones_reported(self):
        artifact = trend_ingest.build_source_candidates(
            DISCOVERY, manual_urls=["https://vimeo.com/123", "not-a-url", ""],
        )
        origins = {row["origin"] for row in artifact["candidates"]}
        assert origins == {"trend_discovery", "manual_url"}
        assert artifact["invalid_manual_urls"] == ["not-a-url", ""]

    def test_duplicate_urls_are_deduplicated(self):
        artifact = trend_ingest.build_source_candidates(
            DISCOVERY, manual_urls=["https://www.youtube.com/watch?v=abc123XYZ_9"],
        )
        urls = [row["candidate_url"] for row in artifact["candidates"]]
        assert len(urls) == len(set(urls))

    def test_empty_discovery_is_insufficient_data_not_failure(self):
        artifact = trend_ingest.build_source_candidates({"videos": []})
        assert artifact["status"] == "insufficient_data"
        assert artifact["candidates"] == []

    def test_panel_never_claims_a_selection(self):
        panel = trend_ingest.format_candidates_panel(
            trend_ingest.build_source_candidates(DISCOVERY)
        )
        assert "secim yapilmadi" in panel
        assert "INDIRME KAPALI" in panel


class TestProbe:
    def test_probe_reads_metadata_without_downloading(self, fake_ytdlp):
        record = trend_ingest.probe_candidate(
            "https://www.youtube.com/watch?v=abc123XYZ_9", ytdlp_module=fake_ytdlp,
        )
        assert record["status"] == "review_required"
        assert record["downloaded"] is False
        assert record["duration_sec"] == 620
        assert record["rights_decision_required"] is True
        assert fake_ytdlp.calls[0]["download"] is False
        assert fake_ytdlp.calls[0]["options"]["skip_download"] is True

    def test_live_source_is_blocked(self, fake_ytdlp):
        fake_ytdlp.info = {**fake_ytdlp.info, "is_live": True}
        record = trend_ingest.probe_candidate("https://x.test/v", ytdlp_module=fake_ytdlp)
        assert record["status"] == "insufficient_data"
        assert any("canli" in blocker for blocker in record["ingest_blockers"])

    def test_probe_failure_is_reported_not_raised(self, fake_ytdlp):
        fake_ytdlp.raise_exc = RuntimeError("Video unavailable")
        record = trend_ingest.probe_candidate("https://x.test/v", ytdlp_module=fake_ytdlp)
        assert record["status"] == "failed"
        assert "unavailable" in record["status_reason"]

    def test_invalid_url_never_reaches_the_downloader(self, fake_ytdlp):
        record = trend_ingest.probe_candidate("javascript:alert(1)", ytdlp_module=fake_ytdlp)
        assert record["status"] == "failed"
        assert fake_ytdlp.calls == []

    def test_signed_url_tokens_are_redacted_from_errors(self, fake_ytdlp):
        fake_ytdlp.raise_exc = RuntimeError(
            "HTTP 403 https://cdn.test/v?signature=SECRETSIG123&token=abcdef123456"
        )
        record = trend_ingest.probe_candidate("https://x.test/v", ytdlp_module=fake_ytdlp)
        assert "SECRETSIG123" not in record["status_reason"]
        assert "abcdef123456" not in record["status_reason"]
        assert "REDACTED" in record["status_reason"]


class TestRightsDecision:
    def test_own_footage_needs_no_attribution(self):
        decision = trend_ingest.build_rights_decision(
            "https://x.test/v", trend_ingest.LICENSE_OWN, "nur",
        )
        assert decision["status"] == "approved"
        assert trend_ingest.validate_rights_decision(decision, "https://x.test/v") == []

    @pytest.mark.parametrize("license_type", trend_ingest.THIRD_PARTY_LICENSE_TYPES)
    def test_third_party_requires_attribution(self, license_type):
        with pytest.raises(ValueError, match="attribution"):
            trend_ingest.build_rights_decision(
                "https://x.test/v", license_type, "nur", risk_acknowledged=True,
            )

    @pytest.mark.parametrize("license_type", trend_ingest.THIRD_PARTY_LICENSE_TYPES)
    def test_third_party_requires_risk_acknowledgement(self, license_type):
        with pytest.raises(ValueError, match="risk_acknowledged"):
            trend_ingest.build_rights_decision(
                "https://x.test/v", license_type, "nur",
                attribution="Kanal - https://x.test/@k",
                transformation_note="Elestirel analiz ve yorum eklendi",
            )

    def test_fair_use_requires_transformation_note(self):
        with pytest.raises(ValueError, match="transformation_note"):
            trend_ingest.build_rights_decision(
                "https://x.test/v", trend_ingest.LICENSE_FAIR_USE_CLAIM, "nur",
                attribution="Kanal - https://x.test/@k", risk_acknowledged=True,
            )

    def test_unknown_license_type_is_refused(self):
        with pytest.raises(ValueError, match="license_type"):
            trend_ingest.build_rights_decision("https://x.test/v", "public_domain_maybe", "nur")

    def test_decided_by_is_mandatory(self):
        with pytest.raises(ValueError, match="decided_by"):
            trend_ingest.build_rights_decision("https://x.test/v", trend_ingest.LICENSE_OWN, " ")

    def test_tampering_with_the_decision_invalidates_it(self, rights_decision):
        rights_decision["decision"]["license_type"] = trend_ingest.LICENSE_OWN
        errors = trend_ingest.validate_rights_decision(rights_decision)
        assert any("GECERSIZ" in error for error in errors)

    def test_decision_for_another_url_does_not_transfer(self, rights_decision):
        errors = trend_ingest.validate_rights_decision(
            rights_decision, "https://www.youtube.com/watch?v=OTHER",
        )
        assert any("BASKA BIR KAYNAK" in error for error in errors)

    def test_missing_decision_is_an_error(self):
        assert trend_ingest.validate_rights_decision(None)
        assert trend_ingest.validate_rights_decision({})


class TestDownloadGate:
    URL = "https://www.youtube.com/watch?v=abc123XYZ_9"

    def test_download_without_rights_is_refused(self, tmp_path, fake_ytdlp):
        with pytest.raises(PermissionError, match="haklar kapisi"):
            trend_ingest.download_source(self.URL, tmp_path, None, ytdlp_module=fake_ytdlp)
        assert fake_ytdlp.calls == []

    def test_download_with_stale_rights_is_refused(self, tmp_path, fake_ytdlp, rights_decision):
        rights_decision["decision"]["attribution"] = "degistirildi"
        with pytest.raises(PermissionError):
            trend_ingest.download_source(
                self.URL, tmp_path, rights_decision, ytdlp_module=fake_ytdlp,
            )
        assert fake_ytdlp.calls == []

    def test_download_with_unapproved_rights_is_refused(self, tmp_path, fake_ytdlp,
                                                        rights_decision):
        rights_decision["status"] = "review_required"
        with pytest.raises(PermissionError):
            trend_ingest.download_source(
                self.URL, tmp_path, rights_decision, ytdlp_module=fake_ytdlp,
            )

    def test_successful_download_produces_review_required_manifest(
        self, tmp_path, fake_ytdlp, rights_decision,
    ):
        probe = trend_ingest.probe_candidate(self.URL, ytdlp_module=fake_ytdlp)
        manifest = trend_ingest.download_source(
            self.URL, tmp_path / "source", rights_decision,
            ytdlp_module=fake_ytdlp, probe_record=probe, downloaded_by="nur",
        )
        assert manifest["status"] == "review_required"
        assert manifest["filesize_bytes"] > 0
        from pathlib import Path

        assert Path(manifest["source_path"]).is_file()
        assert manifest["rights"]["decision_sha256"] == rights_decision["decision_sha256"]
        assert manifest["raw_media_submitted_to_provider"] is False
        assert fake_ytdlp.calls[-1]["download"] is True

    def test_duration_limit_is_enforced_before_downloading(
        self, tmp_path, fake_ytdlp, rights_decision,
    ):
        probe = trend_ingest.probe_candidate(self.URL, ytdlp_module=fake_ytdlp)
        with pytest.raises(ValueError, match="suresi limiti"):
            trend_ingest.download_source(
                self.URL, tmp_path, rights_decision, ytdlp_module=fake_ytdlp,
                probe_record=probe, max_duration_sec=60,
            )
        assert all(call["download"] is False for call in fake_ytdlp.calls)

    def test_download_failure_is_a_failed_manifest_not_an_exception(
        self, tmp_path, fake_ytdlp, rights_decision,
    ):
        fake_ytdlp.raise_exc = RuntimeError("network died token=abcdef123456")
        manifest = trend_ingest.download_source(
            self.URL, tmp_path, rights_decision, ytdlp_module=fake_ytdlp,
        )
        assert manifest["status"] == "failed"
        assert "abcdef123456" not in manifest["error"]

    def test_empty_download_is_not_reported_as_success(
        self, tmp_path, fake_ytdlp, rights_decision,
    ):
        fake_ytdlp.write_bytes = b""
        manifest = trend_ingest.download_source(
            self.URL, tmp_path, rights_decision, ytdlp_module=fake_ytdlp,
        )
        assert manifest["status"] == "failed"

    def test_filesize_limit_is_passed_to_the_downloader(
        self, tmp_path, fake_ytdlp, rights_decision,
    ):
        trend_ingest.download_source(
            self.URL, tmp_path, rights_decision, ytdlp_module=fake_ytdlp,
            max_filesize_bytes=1024 * 1024,
        )
        assert fake_ytdlp.calls[-1]["options"]["max_filesize"] == 1024 * 1024


class TestArtifactWriters:
    def test_artifacts_are_written_atomically(self, tmp_path, rights_decision, read_json):
        candidates = trend_ingest.build_source_candidates(DISCOVERY)
        path = trend_ingest.write_source_candidates(candidates, tmp_path)
        assert read_json(path)["candidate_count"] == 2

        rights_path = trend_ingest.write_rights_decision(rights_decision, tmp_path)
        assert read_json(rights_path)["decided_by"] == "nur"
        assert not list(tmp_path.glob("*.tmp*"))
