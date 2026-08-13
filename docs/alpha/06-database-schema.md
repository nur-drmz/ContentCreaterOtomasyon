# 06 — Database Schema (Logical)

**Formalizes:** `05-domain-model.md` into table shape. Engine-agnostic; `the plan.txt` §16 proposes PostgreSQL as a default assumption, which is **not yet approved** (open decision, §30 "Database hosting").
**Status:** Draft
**Approval needed from:** Eng

---

## 1. Conventions used in this document

- `id`: surrogate primary key, type left open (`UUID` recommended default given multi-tenant + offline-friendly client generation, but not approved).
- `*_id`: foreign key, always NOT NULL unless marked `(nullable)`.
- Every table that is tenant-owned carries a direct or indirectly-resolvable `workspace_id` — required by §19 tenant isolation ("Every tenant-owned record must be scoped to a workspace directly or through a verifiable foreign-key chain").
- Timestamps: `created_at` on every table; `updated_at` where mutable; soft-delete via `deleted_at` only where §17 lifecycle requires recoverability (media assets), hard-delete elsewhere.

## 2. Tenancy tables

```
users(id, email, auth_provider_ref, created_at)
workspaces(id, name, subscription_id, created_at)
workspace_memberships(id, user_id, workspace_id, role, invited_by, joined_at)
  UNIQUE(user_id, workspace_id)
channels(id, workspace_id, external_channel_id, oauth_connection_id, name, language,
         target_audience, status, created_at)
  INDEX(workspace_id)
channel_memberships(id, user_id, channel_id, role)
  UNIQUE(user_id, channel_id)
channel_concepts(id, channel_id, name, description, language, tone,
                 target_duration_min, target_duration_max, spoiler_policy, theory_policy,
                 caption_style_json, editing_rhythm_json, hook_prefs_json, music_prefs_json,
                 character_priorities_json, prompt_profile_json, hot_moment_weights_json,
                 prediction_calibration_json, created_at)
  INDEX(channel_id)
channel_model_versions(id, channel_concept_id, version_number, status,
                        prompt_templates_json, feature_weights_json, scene_ranking_weights_json,
                        script_rules_json, prediction_settings_json, retention_learnings_json,
                        editing_style_constraints_json, published_at, rolled_back_from_id (nullable))
  UNIQUE(channel_concept_id, version_number)
```

## 3. Opportunity and project tables

```
content_opportunities(id, workspace_id, channel_id (nullable), title, entity_ref,
                       opportunity_score, score_breakdown_json, window_state,
                       recommended_channel_id (nullable), recommended_format, created_at, dismissed_at (nullable))
projects(id, workspace_id, channel_id, channel_concept_id, opportunity_id (nullable),
         title, season (nullable), episode (nullable), source_language, target_language,
         output_format, target_duration_seconds, content_mode, spoiler_level, theory_level,
         rights_declaration_id (nullable), state, created_by, created_at, updated_at)
  INDEX(workspace_id), INDEX(channel_id), INDEX(state)
project_assignments(id, project_id, user_id, role_in_project)
  UNIQUE(project_id, user_id)
rights_declarations(id, project_id, declared_by, statement_text, declared_at)
```

## 4. Media and analysis tables

```
media_assets(id, workspace_id, project_id (nullable), asset_type, storage_key, size_bytes,
             content_hash (nullable), retention_expires_at (nullable), deleted_at (nullable), created_at)
  INDEX(workspace_id), INDEX(project_id), INDEX(content_hash)  -- hash index supports dedup (US-3 AC3)
source_videos(id, project_id, media_asset_id, duration_seconds, format, content_hash, upload_status)
upload_sessions(id, source_video_id, storage_key, signed_url_expires_at, chunk_state_json, resumable_token)
video_analyses(id, source_video_id, status, proxy_asset_id (nullable), audio_asset_id (nullable),
               transcript_ref (nullable), shot_boundaries_ref (nullable), scene_boundaries_ref (nullable),
               keyframes_ref (nullable), audio_intensity_curve_ref (nullable), music_rise_curve_ref (nullable),
               completed_at (nullable))
  UNIQUE(source_video_id)  -- enforces "analyze once" (§3.4)
transcript_segments(id, video_analysis_id, start_ts, end_ts, text, speaker_cluster_id (nullable), language)
  INDEX(video_analysis_id)
scenes(id, video_analysis_id, start_ts, end_ts, shot_ids_json, dialogue_segment_ids_json)
  INDEX(video_analysis_id)
scene_genomes(id, scene_id, characters_json, actions_json, emotions_json, audio_json,
              narrative_role, spoiler_level, visual_intensity, theory_potential,
              short_hook_score, vertical_crop_score, reuse_count, is_current)
  INDEX(scene_id)
narrative_nodes(id, video_analysis_id, node_type, label, ref_scene_ids_json)
narrative_edges(id, from_node_id, to_node_id, edge_type)
  INDEX(from_node_id), INDEX(to_node_id)
```

## 5. Moments, prompts, rough cut

```
hot_moments(id, video_analysis_id, scene_genome_id, preview_start, preview_end,
            recommended_core_start, recommended_core_end, reasons_json, hot_moment_score,
            score_breakdown_json, spoiler_level, characters_json, tags_json)
  INDEX(video_analysis_id)
moment_selections(id, project_id, hot_moment_id, status, locked, note (nullable), order_index)
  UNIQUE(project_id, hot_moment_id)
user_prompts(id, project_id, text, resolved_content_mode, created_by, created_at)
prompt_templates(id, workspace_id (nullable), channel_concept_id (nullable), name, content_mode, template_text)
rough_cuts(id, project_id, prompt_id, duration_seconds, format)
timeline_versions(id, rough_cut_id, version_number, clips_json, is_current)
  UNIQUE(rough_cut_id, version_number)
```

## 6. Script and voice

```
script_options(id, project_id, variant, status)
script_sentences(id, script_option_id, order_index, text, timeline_range_start, timeline_range_end,
                 locked, authorship)
evidence_links(id, script_sentence_id, source_moment_id (nullable), source_timestamp (nullable),
               transcript_segment_id (nullable), external_source_ref (nullable), evidence_type, confidence)
voice_overs(id, script_option_id, provider_key, voice_settings_json)
voice_segments(id, voice_over_id, script_sentence_id, audio_asset_id, status, regenerated_at (nullable))
```

## 7. Edit, render, prediction, publication, analytics

```
video_edits(id, project_id, timeline_version_id, captions_json, audio_levels_json,
            transitions_json, sfx_json, status)
final_videos(id, project_id, video_edit_id, media_asset_id, resolution, format)
viral_scores(id, final_video_id, score, sub_scores_json, recommendation_text)
view_predictions(id, final_video_id, window, p10, p50, p90, probability_beat_channel_avg,
                  probability_exceed_threshold, confidence, model_version_ref)
competitor_snapshots(id, content_opportunity_id (nullable), project_id (nullable), source,
                     engagement_metrics_json, captured_at)
publications(id, project_id, channel_id, youtube_video_id (nullable), visibility, uploaded_at (nullable),
             public_approved_by (nullable), public_approved_at (nullable), published_at (nullable))
analytics_snapshots(id, publication_id, captured_at, metrics_json)
retention_segments(id, analytics_snapshot_id, start_pct, end_pct, retention_value)
user_feedback_signals(id, project_id, channel_id, channel_concept_id, signal_type, payload_json, created_at)
  INDEX(channel_id, channel_concept_id)  -- enforces §12 learning isolation
```

## 8. Operational tables

```
jobs(id, job_type, workspace_id, channel_id (nullable), project_id (nullable),
     media_asset_id (nullable), status, correlation_id, cost_recorded_json, created_at)
  INDEX(workspace_id), INDEX(correlation_id), INDEX(status)
job_attempts(id, job_id, attempt_number, started_at, finished_at (nullable), outcome, failure_reason (nullable))
provider_usage(id, workspace_id, provider_type, provider_name, unit_type, quantity, cost,
               project_id (nullable), job_id (nullable), recorded_at)
audit_logs(id, workspace_id, actor_user_id (nullable), action, target_type, target_id,
           metadata_json, created_at)
  INDEX(workspace_id), INDEX(action), INDEX(created_at)
notifications(id, user_id, type, payload_json, read_at (nullable), created_at)
subscriptions(id, workspace_id, plan, status, renews_at)
usage_records(id, workspace_id, period, metric, value)
```

## 9. Tenant isolation enforcement notes

- Every query path must filter by `workspace_id` (directly present) or join through a chain that terminates at `workspace_id` (e.g., `hot_moments` → `video_analyses` → `source_videos` → `projects.workspace_id`). This chain should be enforced at the service layer per §19; row-level security at the DB layer is recommended but depends on the DB engine decision (open, §30).
- `video_analyses` has a UNIQUE constraint on `source_video_id` specifically to make the "analyze once" principle (§3.4) a schema-level guarantee, not just a convention.

## 10. Open decisions

- ID type (UUID vs. ULID vs. auto-increment + tenant-prefixed) — deferred to Eng along with DB engine choice.
- Whether `*_json` columns are native JSON/JSONB (if Postgres) or normalized child tables — recommend JSON for fast-moving weight/config blobs (scoring weights, style JSON) and normalized tables for anything queried/filtered at scale (already modeled that way above: e.g., `transcript_segments`, `narrative_nodes` are normalized, not JSON blobs).
- Vector storage for transcript/scene retrieval (§16 "Vector index") is not modeled as a table here — depends on the "Vector storage approach" open decision (§30); until resolved, treat it as an external index keyed by `transcript_segment.id` / `scene.id`.

## Approval needed from
Eng.
