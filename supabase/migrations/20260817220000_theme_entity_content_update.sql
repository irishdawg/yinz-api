-- Content update for eight fictional_companies_v1 entities: three renamed,
-- six given a logo_url pointing at Supabase Storage (gotiate_assets/icons/).
-- theme_entities was hand-edited directly in the dashboard for this batch
-- (acknowledged departure from "never hand-edited through the API or
-- dashboard" -- see the table's own comment in
-- 20260809232702_theme_content_schema.sql) -- this migration is what makes
-- that content change durable/replayable from a fresh database, and is the
-- forward-migration fix for the drift, not a rewrite of history: it sets
-- final values idempotently regardless of whatever the live table already
-- had from the manual edit.
--
-- theme_data/fictional_companies_v1.json (the offline test suite's fixture
-- -- see AGENTS.md on why tests never touch this table) carries the same
-- eight changes as of this same commit, kept in sync by hand.

update theme_entities set display_name = 'Krumholtz Business Machine'
  where theme_set_id = 'fictional_companies_v1' and theme_set_version = 1 and theme_key = 'fc_004';

update theme_entities set logo_url = 'https://vomnvmnsdhvwtqkjtoal.supabase.co/storage/v1/object/public/gotiate_assets/icons/banana.png'
  where theme_set_id = 'fictional_companies_v1' and theme_set_version = 1 and theme_key = 'fc_007';

update theme_entities set display_name = 'Digler Disposal', logo_url = 'https://vomnvmnsdhvwtqkjtoal.supabase.co/storage/v1/object/public/gotiate_assets/icons/digler.png'
  where theme_set_id = 'fictional_companies_v1' and theme_set_version = 1 and theme_key = 'fc_008';

update theme_entities set logo_url = 'https://vomnvmnsdhvwtqkjtoal.supabase.co/storage/v1/object/public/gotiate_assets/icons/tocksin.png'
  where theme_set_id = 'fictional_companies_v1' and theme_set_version = 1 and theme_key = 'fc_016';

update theme_entities set logo_url = 'https://vomnvmnsdhvwtqkjtoal.supabase.co/storage/v1/object/public/gotiate_assets/icons/hertzman.png'
  where theme_set_id = 'fictional_companies_v1' and theme_set_version = 1 and theme_key = 'fc_024';

update theme_entities set display_name = 'Wunderbar Sausage'
  where theme_set_id = 'fictional_companies_v1' and theme_set_version = 1 and theme_key = 'fc_026';

update theme_entities set logo_url = 'https://vomnvmnsdhvwtqkjtoal.supabase.co/storage/v1/object/public/gotiate_assets/icons/pinebox.png'
  where theme_set_id = 'fictional_companies_v1' and theme_set_version = 1 and theme_key = 'fc_032';

update theme_entities set logo_url = 'https://vomnvmnsdhvwtqkjtoal.supabase.co/storage/v1/object/public/gotiate_assets/icons/feral.png'
  where theme_set_id = 'fictional_companies_v1' and theme_set_version = 1 and theme_key = 'fc_036';
