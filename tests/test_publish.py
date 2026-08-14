"""Publication gates and the YouTube provider. No network, no real credential."""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from local_pipeline import publish, publish_youtube


def make_intent(render, metadata, ingest=None, **target_kwargs):
    target = publish.PublishTarget(platform=publish.PLATFORM_YOUTUBE, **target_kwargs)
    return publish.build_publish_intent(render, metadata, target, ingest_manifest=ingest)


class TestPublishTarget:
    def test_invalid_visibility_is_refused(self):
        with pytest.raises(ValueError, match="visibility"):
            publish.PublishTarget(platform="youtube", visibility="everyone")

    def test_scheduled_target_must_stay_private(self):
        with pytest.raises(ValueError, match="private"):
            publish.PublishTarget(
                platform="youtube", visibility="public",
                publish_at="2026-09-01T10:00:00Z",
            )

    def test_platform_is_mandatory(self):
        with pytest.raises(ValueError, match="platform"):
            publish.PublishTarget(platform="")


class TestPreconditions:
    def test_approved_render_with_approved_metadata_passes(self, approved_render,
                                                           approved_metadata):
        assert publish.validate_publish_preconditions(approved_render, approved_metadata) == []

    def test_unapproved_render_is_blocked(self, approved_render, approved_metadata):
        approved_render["status"] = "review_required"
        errors = publish.validate_publish_preconditions(approved_render, approved_metadata)
        assert any("render onayli degil" in error for error in errors)

    def test_rerendered_video_invalidates_publication(self, approved_render,
                                                      approved_metadata, video_file):
        video_file.write_bytes(b"different-bytes")
        errors = publish.validate_publish_preconditions(approved_render, approved_metadata)
        assert any("gecersiz" in error for error in errors)

    def test_revoked_upstream_condition_blocks_publication(self, approved_render,
                                                           approved_metadata):
        approved_render["approval"]["conditions"]["moment_selection_approved"] = False
        errors = publish.validate_publish_preconditions(approved_render, approved_metadata)
        assert any("gecersiz" in error for error in errors)

    def test_unapproved_metadata_is_blocked(self, approved_render, approved_metadata):
        approved_metadata["status"] = "review_required"
        errors = publish.validate_publish_preconditions(approved_render, approved_metadata)
        assert any("metadatasi onayli degil" in error for error in errors)

    def test_missing_metadata_is_blocked(self, approved_render):
        errors = publish.validate_publish_preconditions(approved_render, None)
        assert any("metadatasi yok" in error for error in errors)

    def test_missing_video_file_is_blocked(self, approved_render, approved_metadata,
                                          video_file):
        video_file.unlink()
        errors = publish.validate_publish_preconditions(approved_render, approved_metadata)
        assert errors

    def test_third_party_ingest_without_rights_owner_is_blocked(
        self, approved_render, approved_metadata, ingest_manifest,
    ):
        ingest_manifest["rights"]["decided_by"] = None
        errors = publish.validate_publish_preconditions(
            approved_render, approved_metadata, ingest_manifest,
        )
        assert any("decided_by" in error for error in errors)


class TestPublishIntentGate:
    def test_intent_is_born_review_required_and_private(self, approved_render,
                                                        approved_metadata):
        intent = make_intent(approved_render, approved_metadata, visibility="public")
        assert intent["status"] == "review_required"
        assert intent["decision"]["initial_visibility"] == publish.INITIAL_PRIVACY
        assert intent["decision"]["requested_visibility"] == "public"
        assert intent["upload_performed"] is False

    def test_intent_cannot_be_created_when_preconditions_fail(self, approved_render,
                                                              approved_metadata):
        approved_render["status"] = "review_required"
        with pytest.raises(ValueError, match="OLUSTURULAMADI"):
            make_intent(approved_render, approved_metadata)

    def test_wrong_hash_is_refused(self, approved_render, approved_metadata):
        intent = make_intent(approved_render, approved_metadata)
        with pytest.raises(ValueError, match="ONAY REDDEDILDI"):
            publish.approve_publish_intent(intent, "0" * 64, "nur")

    def test_approval_requires_a_person(self, approved_render, approved_metadata):
        intent = make_intent(approved_render, approved_metadata)
        with pytest.raises(ValueError, match="approved_by"):
            publish.approve_publish_intent(intent, intent["decision_sha256"], "")

    def test_editing_metadata_after_approval_invalidates_it(self, approved_render,
                                                            approved_metadata):
        intent = make_intent(approved_render, approved_metadata)
        approved = publish.approve_publish_intent(intent, intent["decision_sha256"], "nur")
        assert publish.check_publish_intent_approval(approved)["valid"] is True
        approved["decision"]["metadata"]["title"] = "gizlice degistirildi"
        assert publish.check_publish_intent_approval(approved)["valid"] is False


class TestPrivateUploadExecution:
    def _approved_intent(self, render, metadata, ingest=None, **kwargs):
        intent = make_intent(render, metadata, ingest, **kwargs)
        return publish.approve_publish_intent(intent, intent["decision_sha256"], "nur")

    def test_unapproved_intent_cannot_upload(self, approved_render, approved_metadata,
                                            youtube_provider):
        intent = make_intent(approved_render, approved_metadata)
        with pytest.raises(PermissionError, match="REDDEDILDI"):
            publish.execute_private_upload(intent, provider=youtube_provider)
        assert youtube_provider._http.requests == []

    def test_changed_video_bytes_block_upload(self, approved_render, approved_metadata,
                                             youtube_provider, video_file):
        intent = self._approved_intent(approved_render, approved_metadata)
        video_file.write_bytes(b"swapped-after-approval")
        with pytest.raises(PermissionError, match="degismis"):
            publish.execute_private_upload(intent, provider=youtube_provider)

    def test_upload_sends_private_and_records_quota(self, approved_render,
                                                    approved_metadata, ingest_manifest,
                                                    youtube_provider):
        intent = self._approved_intent(
            approved_render, approved_metadata, ingest_manifest, visibility="public",
        )
        manifest = publish.execute_private_upload(
            intent, provider=youtube_provider, ingest_manifest=ingest_manifest,
            uploaded_by="nur",
        )
        assert manifest["upload"]["status"] == "review_required"
        assert manifest["upload"]["remote_id"] == "newVideoId123"
        assert manifest["current_visibility"] == "private"
        assert manifest["requested_visibility"] == "public"
        assert manifest["upload"]["quota_units_estimated"] == 1600
        assert manifest["rights"]["decided_by"] == "nur"
        assert manifest["visibility_approval"] is None

        init_request = youtube_provider._http.requests[0]
        assert init_request["json"]["status"]["privacyStatus"] == "private"

    def test_an_intent_can_only_be_used_once(self, approved_render, approved_metadata,
                                             youtube_provider):
        intent = self._approved_intent(approved_render, approved_metadata)
        publish.execute_private_upload(intent, provider=youtube_provider)
        intent["upload_performed"] = True
        with pytest.raises(PermissionError, match="zaten kullanilmis"):
            publish.execute_private_upload(intent, provider=youtube_provider)


class TestVisibilityGate:
    def _manifest(self, render, metadata, provider):
        intent = make_intent(render, metadata)
        intent = publish.approve_publish_intent(intent, intent["decision_sha256"], "nur")
        return publish.execute_private_upload(intent, provider=provider)

    def test_visibility_change_without_approval_is_refused(self, approved_render,
                                                            approved_metadata,
                                                            youtube_provider):
        manifest = self._manifest(approved_render, approved_metadata, youtube_provider)
        with pytest.raises(PermissionError, match="REDDEDILDI"):
            publish.execute_visibility_change(manifest, provider=youtube_provider)

    def test_visibility_approval_requires_the_reviewed_remote_id(self, approved_render,
                                                                  approved_metadata,
                                                                  youtube_provider):
        manifest = self._manifest(approved_render, approved_metadata, youtube_provider)
        with pytest.raises(ValueError, match="eslesmiyor"):
            publish.approve_visibility(manifest, "public", "someOtherId", "nur")

    def test_approved_visibility_is_applied(self, approved_render, approved_metadata,
                                           youtube_provider):
        manifest = self._manifest(approved_render, approved_metadata, youtube_provider)
        manifest = publish.approve_visibility(manifest, "public", "newVideoId123", "nur")
        updated = publish.execute_visibility_change(manifest, provider=youtube_provider)
        assert updated["current_visibility"] == "public"
        assert updated["status"] == "production_ready"

    def test_scheduling_keeps_the_video_private(self, approved_render, approved_metadata,
                                               youtube_provider):
        manifest = self._manifest(approved_render, approved_metadata, youtube_provider)
        manifest = publish.approve_visibility(
            manifest, "private", "newVideoId123", "nur",
            publish_at="2026-09-01T10:00:00Z",
        )
        updated = publish.execute_visibility_change(manifest, provider=youtube_provider)
        assert updated["current_visibility"] == "private"
        last = youtube_provider._http.requests[-1]
        assert last["json"]["status"]["publishAt"] == "2026-09-01T10:00:00Z"
        assert last["json"]["status"]["privacyStatus"] == "private"

    def test_mismatched_target_visibility_is_refused(self, approved_render,
                                                     approved_metadata, youtube_provider):
        manifest = self._manifest(approved_render, approved_metadata, youtube_provider)
        manifest = publish.approve_visibility(manifest, "unlisted", "newVideoId123", "nur")
        allowed, reason = publish.visibility_change_allowed(manifest, "public")
        assert allowed is False
        assert "unlisted" in reason


class TestProviderRegistry:
    def test_planned_platform_is_honest_about_not_existing(self):
        with pytest.raises(NotImplementedError, match="planlandi"):
            publish.resolve_provider("tiktok")

    def test_unknown_platform_is_rejected(self):
        with pytest.raises(ValueError, match="bilinmeyen"):
            publish.resolve_provider("myspace")

    def test_registration_round_trip(self, youtube_provider):
        publish.register_provider(youtube_provider)
        assert publish.resolve_provider("youtube") is youtube_provider
        assert "youtube" in publish.registered_platforms()


class TestYouTubeProviderInternals:
    def test_video_resource_is_always_private(self, approved_metadata):
        target = publish.PublishTarget(platform="youtube", visibility="public")
        body = publish_youtube.build_video_resource(
            {"title": "Baslik burada", "description": "aciklama", "tags": ["a", "b"]}, target,
        )
        assert body["status"]["privacyStatus"] == "private"

    def test_empty_title_is_refused(self):
        target = publish.PublishTarget(platform="youtube")
        with pytest.raises(ValueError, match="basligi bos"):
            publish_youtube.build_video_resource({"title": "  "}, target)

    def test_overlong_title_is_refused(self):
        target = publish.PublishTarget(platform="youtube")
        with pytest.raises(ValueError, match="karakteri asiyor"):
            publish_youtube.build_video_resource({"title": "x" * 101}, target)

    def test_tags_are_trimmed_to_the_total_character_budget(self):
        target = publish.PublishTarget(platform="youtube")
        body = publish_youtube.build_video_resource(
            {"title": "Gecerli baslik", "tags": ["y" * 200] * 5}, target,
        )
        total = sum(len(tag) + 1 for tag in body["snippet"]["tags"])
        assert total <= publish_youtube.MAX_TAGS_TOTAL_CHARS

    def test_chunk_size_is_aligned_to_256_kib(self, client_secrets, token_store):
        provider = publish_youtube.YouTubeProvider(
            client_secrets=client_secrets, token_store=token_store,
            chunk_bytes=300_000,
        )
        assert provider._chunk_bytes % (256 * 1024) == 0

    def test_resumable_upload_resumes_from_the_server_range(self, client_secrets,
                                                            token_store, tmp_path,
                                                            scripted_resumable_http):
        token_store.save("fake-refresh-token")
        video = tmp_path / "v.mp4"
        video.write_bytes(b"z" * (600 * 1024))
        http = scripted_resumable_http(accept_first_bytes=256 * 1024)
        provider = publish_youtube.YouTubeProvider(
            client_secrets=client_secrets, token_store=token_store, http=http,
            http_post=lambda url, data: {"access_token": "a", "expires_in": 3600},
            chunk_bytes=256 * 1024,
        )
        result = provider.upload_private(
            video, {"title": "Gecerli baslik"},
            publish.PublishTarget(platform="youtube"),
        )
        assert result.status == "review_required"
        assert result.remote_id == "resumedVideo"
        assert http.offsets == [0, 262144, 524288]

    def test_upload_failure_is_reported_not_raised(self, client_secrets, token_store,
                                                   tmp_path, fake_http, fake_response):
        token_store.save("fake-refresh-token")
        video = tmp_path / "v.mp4"
        video.write_bytes(b"z" * 1024)
        http = fake_http(lambda method, url, **kw: fake_response(
            403, {"error": {"message": "quotaExceeded"}},
        ))
        provider = publish_youtube.YouTubeProvider(
            client_secrets=client_secrets, token_store=token_store, http=http,
            http_post=lambda url, data: {"access_token": "a", "expires_in": 3600},
        )
        result = provider.upload_private(
            video, {"title": "Gecerli baslik"}, publish.PublishTarget(platform="youtube"),
        )
        assert result.status == "failed"
        assert "quotaExceeded" in result.error

    def test_missing_file_is_reported(self, youtube_provider, tmp_path):
        result = youtube_provider.upload_private(
            tmp_path / "nope.mp4", {"title": "Gecerli baslik"},
            publish.PublishTarget(platform="youtube"),
        )
        assert result.status == "failed"


class TestCredentialHygiene:
    def test_token_file_is_owner_only(self, token_store):
        import stat

        token_store.save("super-secret-refresh-token")
        mode = stat.S_IMODE(token_store.path.stat().st_mode)
        assert mode == 0o600

    def test_state_never_reveals_the_token(self, token_store):
        token_store.save("super-secret-refresh-token", channel_title="Kanal")
        state = token_store.describe_state()
        assert state["configured"] is True
        assert "super-secret-refresh-token" not in str(state)

    def test_provider_state_never_reveals_credentials(self, youtube_provider):
        state = youtube_provider.describe_credential_state()
        blob = str(state)
        assert "fake-refresh-token" not in blob
        assert "test-client-secret-value" not in blob
        assert state["upload_quota_units_per_video"] == 1600
        assert state["estimated_uploads_per_day"] == 6

    def test_empty_refresh_token_is_refused(self, token_store):
        with pytest.raises(publish_youtube.PublishAuthError):
            token_store.save("")

    def test_missing_authorization_is_actionable(self, client_secrets, token_store):
        provider = publish_youtube.YouTubeProvider(
            client_secrets=client_secrets, token_store=token_store,
        )
        with pytest.raises(publish_youtube.PublishAuthError, match="yetkilendirme"):
            provider._tokens().access_token()

    def test_corrupt_token_file_is_treated_as_unauthorized(self, token_store):
        token_store.path.parent.mkdir(parents=True, exist_ok=True)
        token_store.path.write_text("{not json", encoding="utf-8")
        assert token_store.describe_state()["configured"] is False

    def test_authorization_url_forces_offline_consent(self, client_secrets):
        url = publish_youtube.build_authorization_url(client_secrets, "http://localhost:8765/cb")
        assert "access_type=offline" in url
        assert "prompt=consent" in url
        assert "youtube.upload" in url

    def test_code_exchange_without_refresh_token_is_refused(self, client_secrets):
        with pytest.raises(publish_youtube.PublishAuthError, match="refresh_token"):
            publish_youtube.exchange_code(
                client_secrets, "code123", "http://localhost",
                http_post=lambda url, data: {"access_token": "a", "expires_in": 3600},
            )

    def test_error_sanitizer_strips_tokens(self):
        text = publish_youtube.sanitize_publish_error(
            'invalid_grant access_token=ya29.SECRETVALUE123 refresh_token=1//SECRET456'
        )
        assert "ya29.SECRETVALUE123" not in text
        assert "1//SECRET456" not in text

    def test_missing_client_secrets_path_is_actionable(self):
        with pytest.raises(publish_youtube.PublishAuthError, match="YOUTUBE_OAUTH_CLIENT_SECRETS_PATH"):
            publish_youtube.load_client_secrets(environ={})


class TestManifestChain:
    def test_manifest_records_the_full_approval_chain(self, approved_render,
                                                      approved_metadata, ingest_manifest,
                                                      youtube_provider, tmp_path,
                                                      read_json):
        intent = make_intent(approved_render, approved_metadata, ingest_manifest)
        intent = publish.approve_publish_intent(intent, intent["decision_sha256"], "nur")
        manifest = publish.execute_private_upload(
            intent, provider=youtube_provider, ingest_manifest=ingest_manifest,
        )
        path = publish.write_publish_manifest(manifest, tmp_path)
        saved = read_json(path)
        chain = saved["approval_chain"]
        assert chain["publish_approved_by"] == "nur"
        video_bytes = Path(approved_render["output"]["final_video_path"]).read_bytes()
        assert chain["video_sha256"] == hashlib.sha256(video_bytes).hexdigest()
        assert chain["rights_decision_sha256"] == ingest_manifest["rights"]["decision_sha256"]
