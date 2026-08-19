-- Content update: fc_031 (Oxide Tractor Co.) given a logo_url pointing at
-- Supabase Storage (gotiate_assets/icons/). Same pattern as
-- 20260817220000_theme_entity_content_update.sql.
--
-- theme_data/fictional_companies_v1.json (the offline test suite's
-- fixture -- see AGENTS.md on why tests never touch this table) carries
-- the same change as of this same commit, kept in sync by hand.

update theme_entities set logo_url = 'https://vomnvmnsdhvwtqkjtoal.supabase.co/storage/v1/object/public/gotiate_assets/icons/tractor.png'
  where theme_set_id = 'fictional_companies_v1' and theme_set_version = 1 and theme_key = 'fc_031';
