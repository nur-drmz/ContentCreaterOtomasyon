# 05 — Domain Model

**Formalizes:** `the plan.txt` §21 (Core Domain Entities), §4 (Multi-Tenant Product Structure), §9 (Scene Genome), §10 (Narrative Graph), §11 (Evidence Links).
**Status:** Draft
**Approval needed from:** Eng

---

This is a logical domain model (entities, key fields, relationships). It intentionally avoids database-engine syntax — see `06-database-schema.md` for the physical mapping.

## 1. Tenancy backbone

- **User** — id, email, auth identity, created_at.
- **Workspace** — id, name, subscription_id, created_at. Root tenant boundary (§4.1).
- **WorkspaceMembership** — user_id, workspace_id, workspace_role (§3 roles), invited_by, joined_at.
- **Channel** — id, workspace_id, external_channel_id, oauth_connection_id, name, language, target_audience, status, performance_baseline_ref.
- **ChannelMembership** — user_id, channel_id, channel_role.
- **ChannelConcept** — id, channel_id, name, description, language, tone, target_duration_range, spoiler_policy, theory_policy, caption_style, editing_rhythm, hook_preferences, music_preferences, character_priorities, prompt_profile, hot_moment_weights_ref, prediction_calibration_ref (§4.3).
- **ChannelModelVersion** — id, channel_concept_id, version_number, status (draft/published), prompt_templates, feature_weights, scene_ranking_weights, script_rules, prediction_settings, retention_learnings, editing_style_constraints, published_at, rolled_back_from_id (§4.4).

## 2. Opportunity and project

- **ContentOpportunity** — id, workspace_id, channel_id (nullable until assigned), title/entity, opportunity_score, score_breakdown (§7 weights), window_state (emerging/near_peak/decaying/saturated/etc.), recommended_channel_id, recommended_format, created_at, dismissed_at.
- **Project** — id, workspace_id, channel_id, channel_concept_id, opportunity_id (nullable), title, season, episode, source_language, target_language, output_format, target_duration, content_mode, spoiler_level, theory_level, rights_declaration_id, state (§04 state machine), created_by.
- **ProjectAssignment** — project_id, user_id, role_in_project.
- **RightsDeclaration** — id, project_id, declared_by, statement_text (must not claim legal clearance per §3.2), declared_at.

## 3. Source media and analysis

- **SourceVideo** — id, project_id, media_asset_id, duration, format, content_hash, upload_status.
- **UploadSession** — id, source_video_id, storage_key, signed_url_expires_at, chunk_state, resumable_token.
- **MediaAsset** — id, workspace_id, project_id, asset_type (source/proxy/audio/subtitle/keyframe/preview/clip/render/thumbnail — §17), storage_key, size, retention_expires_at, deleted_at.
- **VideoAnalysis** — id, source_video_id, status, proxy_asset_id, audio_asset_id, transcript_ref, shot_boundaries_ref, scene_boundaries_ref, keyframes_ref, audio_intensity_curve_ref, music_rise_curve_ref, completed_at. One per source video; reused across the project's lifetime (§3.4).
- **TranscriptSegment** — id, video_analysis_id, start_ts, end_ts, text, speaker_cluster_id (nullable), language.
- **Scene** — id, video_analysis_id, start_ts, end_ts, shot_ids, dialogue_segment_ids.
- **SceneGenome** — id, scene_id, characters[], actions[], emotions[], audio {music_rise, dialogue, impact}, narrative_role, spoiler_level, visual_intensity, theory_potential, short_hook_score, vertical_crop_score, reuse_count (§9 — matches the JSON example verbatim).
- **NarrativeNode** — id, video_analysis_id, node_type (character/goal/conflict/event/decision/reveal/consequence/relationship/unresolved_question/location — §10), label, ref_scene_ids[].
- **NarrativeEdge** — id, from_node_id, to_node_id, edge_type (causes/follows/contradicts/supports/weakens/parallels/foreshadows/resolves/escalates — §10).

## 4. Moments, prompts, rough cut

- **HotMoment** — id, video_analysis_id, scene_genome_id, preview_start, preview_end, recommended_core_start, recommended_core_end, reasons[], hot_moment_score, score_breakdown (§8 weights), spoiler_level, characters[], tags[].
- **MomentSelection** — id, project_id, hot_moment_id, status (selected/favorited/rejected), locked, note, order_index.
- **UserPrompt** — id, project_id, text, resolved_content_mode, created_by.
- **PromptTemplate** — id, workspace_id (nullable = global), channel_concept_id (nullable), name, content_mode, template_text.
- **RoughCut** — id, project_id, prompt_id, duration, format.
- **TimelineVersion** — id, rough_cut_id, version_number, clips_json (matches §Stage 8 JSON shape: momentId, sourceStart, sourceEnd, timelineStart, cropMode, originalAudio), is_current.

## 5. Script and voice

- **ScriptOption** — id, project_id, variant (viral/analytical/cinematic), status.
- **ScriptSentence** — id, script_option_id, order_index, text, timeline_range_start, timeline_range_end, locked, authorship (user/ai).
- **EvidenceLink** — id, script_sentence_id, source_moment_id (nullable), source_timestamp (nullable), transcript_segment_id (nullable), external_source_ref (nullable), evidence_type (confirmed/inference/interpretation/speculative — §3.6, §11), confidence.
- **VoiceOver** — id, script_option_id, provider (interface-typed, §3.7), voice_settings_json.
- **VoiceSegment** — id, voice_over_id, script_sentence_id, audio_asset_id, status, regenerated_at.

## 6. Edit, render, prediction, publication

- **VideoEdit** — id, project_id, timeline_version_id, captions_json, audio_levels_json, transitions_json, sfx_json, status.
- **FinalVideo** — id, project_id, video_edit_id, media_asset_id, resolution, format.
- **ViralScore** — id, final_video_id (or pre-render variant id), score, sub_scores_json (hook/topic_timing/visual_pacing/originality/competition), recommendation_text.
- **ViewPrediction** — id, final_video_id, window (24h/7d), p10, p50, p90, probability_beat_channel_avg, probability_exceed_threshold, confidence, model_version_ref (§14).
- **CompetitorSnapshot** — id, content_opportunity_id or project_id, source, engagement_metrics_json, captured_at.
- **Publication** — id, project_id, channel_id, youtube_video_id, visibility (private/unlisted/public), uploaded_at, public_approved_by, public_approved_at, published_at.

## 7. Analytics, feedback, operations

- **AnalyticsSnapshot** — id, publication_id, captured_at, metrics_json (views, retention curve, completion rate, likes, comments, shares, subscriber_conversion, traffic_sources — §Stage 16).
- **RetentionSegment** — id, analytics_snapshot_id, start_pct, end_pct, retention_value.
- **UserFeedbackSignal** — id, project_id, signal_type (moment_selected/moment_rejected/script_edited/timeline_edited/clip_replaced/...), payload_json, created_at. Feeds Channel Brain learning (§12), scoped by channel_id + channel_concept_id (§12 isolation rule).
- **Job** — id, job_type (§08 job spec), workspace_id, channel_id, project_id, media_asset_id (nullable), status, correlation_id, cost_recorded_json.
- **JobAttempt** — id, job_id, attempt_number, started_at, finished_at, outcome, failure_reason.
- **ProviderUsage** — id, workspace_id, provider_type (llm/vision/stt/tts/storage/render — §3.7), provider_name, unit_type, quantity, cost, project_id, job_id.
- **AuditLog** — id, workspace_id, actor_user_id, action, target_type, target_id, metadata_json, created_at (§19 event list).
- **Notification** — id, user_id, type, payload_json, read_at.
- **Subscription** — id, workspace_id, plan, status, renews_at.
- **UsageRecord** — id, workspace_id, period, metric, value.

## 8. Key relationships summary

```
Workspace 1--N Channel 1--N ChannelConcept 1--N ChannelModelVersion
Workspace 1--N Project N--1 Channel, N--1 ChannelConcept
Project 1--1 SourceVideo 1--1 VideoAnalysis 1--N (Scene, TranscriptSegment, NarrativeNode/Edge, HotMoment via SceneGenome)
Project 1--N MomentSelection --N HotMoment
Project 1--N UserPrompt 1--N RoughCut 1--N TimelineVersion
Project 1--N ScriptOption 1--N ScriptSentence 1--N EvidenceLink
ScriptOption 1--1 VoiceOver 1--N VoiceSegment
Project 1--N VideoEdit 1--1 FinalVideo 1--1 Publication
Publication 1--N AnalyticsSnapshot 1--N RetentionSegment
```

## 9. Open decisions

- Whether `Scene`/`SceneGenome` are 1:1 or `SceneGenome` can be regenerated as a new version against the same `Scene` (versioning would matter for `reuseCount` accuracy across model-weight changes) — recommend 1:N with a `current` flag, pending Eng review.
- Character identity resolution (a durable `Character` entity vs. free-text labels inside `SceneGenome.characters[]`) is intentionally left as free-text for MVP since automatic unconfirmed character recognition is out of scope (§23 excluded, §31 non-goal); revisit in Phase 1.1.

## Approval needed from
Eng.
