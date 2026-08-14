"""Publication metadata: generation, attribution, selection, approval."""
from __future__ import annotations

import pytest

from local_pipeline import metadata_studio


def route_result(payload, status="ok"):
    return {"status": status, "provider": "google", "model": "gemini-3.5-flash-lite",
            "output": payload}


GOOD_PAYLOAD = {
    "titles": [
        "Bu sahnede gozden kacan detay",
        "Kimse bu ani fark etmedi",
        "Sahnenin gercek anlami",
    ],
    "description": "Sahneyi kare kare inceliyoruz ve neyin degistigini gosteriyoruz.",
    "tags": ["analiz", "sahne", "analiz"],
    "hashtags": ["shorts", "#analiz"],
}


class TestPrompt:
    def test_prompt_requires_real_input(self):
        with pytest.raises(ValueError, match="zorunlu"):
            metadata_studio.build_metadata_prompt()

    def test_prompt_asks_for_three_variants_and_forbids_false_clickbait(self):
        prompt = metadata_studio.build_metadata_prompt(script_text="Bir sahne anlatimi")
        assert "3 farkli baslik" in prompt
        assert "Clickbait yalan kullanma" in prompt


class TestGeneration:
    def test_generation_produces_review_required_variants(self):
        artifact = metadata_studio.generate_metadata(
            script_text="Bir sahne anlatimi",
            generate_fn=lambda prompt, **kw: route_result(GOOD_PAYLOAD),
        )
        assert artifact["status"] == "review_required"
        assert len(artifact["title_variants"]) == 3
        assert artifact["tags"] == ["analiz", "sahne"]
        assert artifact["selected"] is None

    def test_json_string_response_is_accepted(self):
        import json

        artifact = metadata_studio.generate_metadata(
            script_text="Bir sahne anlatimi",
            generate_fn=lambda prompt, **kw: route_result(
                "```json\n" + json.dumps(GOOD_PAYLOAD) + "\n```"
            ),
        )
        assert artifact["status"] == "review_required"

    def test_empty_title_is_never_accepted(self):
        artifact = metadata_studio.generate_metadata(
            script_text="Bir sahne anlatimi",
            generate_fn=lambda prompt, **kw: route_result({"titles": ["", "  "], "description": "x"}),
        )
        assert artifact["status"] == "failed"
        assert artifact["title_variants"] == []

    def test_provider_failure_never_fabricates_metadata(self):
        artifact = metadata_studio.generate_metadata(
            script_text="Bir sahne anlatimi",
            generate_fn=lambda prompt, **kw: {"status": "failed", "status_reason": "429"},
        )
        assert artifact["status"] == "failed"
        assert artifact["selected"] is None

    def test_missing_credential_is_insufficient_data_not_failure(self):
        artifact = metadata_studio.generate_metadata(
            script_text="Bir sahne anlatimi",
            generate_fn=lambda prompt, **kw: {
                "status": "insufficient_data", "status_reason": "kimlik bilgisi yok",
            },
        )
        assert artifact["status"] == "insufficient_data"

    def test_provider_exception_is_captured(self):
        def boom(prompt, **kw):
            raise RuntimeError("router patladi")

        artifact = metadata_studio.generate_metadata(
            script_text="Bir sahne anlatimi", generate_fn=boom,
        )
        assert artifact["status"] == "failed"
        assert "router patladi" in artifact["error"]

    def test_forbidden_characters_are_stripped(self):
        artifact = metadata_studio.generate_metadata(
            script_text="x",
            generate_fn=lambda prompt, **kw: route_result({
                "titles": ["<script>alert</script> gecerli baslik"],
                "description": "aciklama <b>kalin</b>",
            }),
        )
        assert "<" not in artifact["title_variants"][0]
        assert "<" not in artifact["description"]


class TestAttribution:
    def test_own_footage_gets_no_attribution_block(self):
        assert metadata_studio.build_attribution_block(
            {"rights": {"license_type": "own"}}
        ) == ""

    def test_third_party_block_contains_source_and_license(self, ingest_manifest):
        block = metadata_studio.build_attribution_block(ingest_manifest)
        assert metadata_studio.ATTRIBUTION_HEADER in block
        assert "Ornek Kanal" in block
        assert "fair_use_claim" in block

    def test_apply_attribution_is_idempotent(self, ingest_manifest):
        artifact = metadata_studio.build_manual_metadata(title="Gecerli baslik burada")
        once = metadata_studio.apply_attribution(artifact, ingest_manifest)
        twice = metadata_studio.apply_attribution(once, ingest_manifest)
        assert once["description"] == twice["description"]
        assert once["attribution_required"] is True
        assert once["attribution_applied"] is True

    def test_applying_attribution_resets_a_previous_selection(self, ingest_manifest):
        artifact = metadata_studio.build_manual_metadata(title="Gecerli baslik burada")
        artifact = metadata_studio.select_metadata(artifact, 0)
        artifact = metadata_studio.apply_attribution(artifact, ingest_manifest)
        assert artifact["selected"] is None
        assert artifact["status"] == "review_required"

    def test_stripping_the_attribution_block_fails_validation(self, ingest_manifest):
        artifact = metadata_studio.build_manual_metadata(title="Gecerli baslik burada")
        artifact = metadata_studio.apply_attribution(artifact, ingest_manifest)
        artifact = metadata_studio.select_metadata(
            artifact, 0, description_override="attribution silindi",
        )
        errors = metadata_studio.validate_metadata(artifact)
        assert any("attribution" in error for error in errors)


class TestSelectionAndApproval:
    def _artifact(self):
        return metadata_studio.generate_metadata(
            script_text="x", generate_fn=lambda prompt, **kw: route_result(GOOD_PAYLOAD),
        )

    def test_selection_requires_a_valid_index(self):
        artifact = self._artifact()
        with pytest.raises(ValueError, match="gecersiz title_index"):
            metadata_studio.select_metadata(artifact, 9)

    def test_selection_binds_a_hash(self):
        artifact = metadata_studio.select_metadata(self._artifact(), 1)
        assert artifact["selected"]["title"] == GOOD_PAYLOAD["titles"][1]
        assert artifact["selected_sha256"]
        assert metadata_studio.validate_metadata(artifact) == []

    def test_unselected_metadata_cannot_be_approved(self):
        with pytest.raises(ValueError, match="secim"):
            metadata_studio.approve_metadata(self._artifact(), "x" * 64, "nur")

    def test_wrong_hash_is_refused(self):
        artifact = metadata_studio.select_metadata(self._artifact(), 0)
        with pytest.raises(ValueError, match="ONAY REDDEDILDI"):
            metadata_studio.approve_metadata(artifact, "0" * 64, "nur")

    def test_approval_requires_a_person(self):
        artifact = metadata_studio.select_metadata(self._artifact(), 0)
        with pytest.raises(ValueError, match="approved_by"):
            metadata_studio.approve_metadata(artifact, artifact["selected_sha256"], "")

    def test_correct_hash_approves(self):
        artifact = metadata_studio.select_metadata(self._artifact(), 0)
        approved = metadata_studio.approve_metadata(
            artifact, artifact["selected_sha256"], "nur",
        )
        assert approved["status"] == "approved"
        assert approved["metadata_approval"]["approved_by"] == "nur"

    def test_reselecting_drops_a_previous_approval(self):
        artifact = metadata_studio.select_metadata(self._artifact(), 0)
        approved = metadata_studio.approve_metadata(
            artifact, artifact["selected_sha256"], "nur",
        )
        reselected = metadata_studio.select_metadata(approved, 2)
        assert reselected["status"] == "review_required"
        assert "metadata_approval" not in reselected

    def test_tampering_with_the_selection_invalidates_it(self):
        artifact = metadata_studio.select_metadata(self._artifact(), 0)
        artifact["selected"]["title"] = "gizlice degistirildi"
        errors = metadata_studio.validate_metadata(artifact)
        assert any("degismis" in error for error in errors)


class TestManualPath:
    def test_short_title_is_refused(self):
        with pytest.raises(ValueError, match="en az"):
            metadata_studio.build_manual_metadata(title="kisa")

    def test_manual_metadata_still_needs_approval(self):
        artifact = metadata_studio.build_manual_metadata(title="Gecerli baslik burada")
        assert artifact["status"] == "review_required"
        assert artifact["origin"] == "manual"

    def test_duplicate_tags_are_removed(self):
        artifact = metadata_studio.build_manual_metadata(
            title="Gecerli baslik burada", tags=["a", "A", "#a", "b"],
        )
        assert artifact["tags"] == ["a", "b"]


class TestWriter:
    def test_metadata_is_written(self, tmp_path, read_json):
        artifact = metadata_studio.build_manual_metadata(title="Gecerli baslik burada")
        path = metadata_studio.write_metadata(artifact, tmp_path)
        assert read_json(path)["artifact_kind"] == "publication_metadata"
