-- Content update for five fictional_companies_v1 entities: four given a
-- logo_url pointing at Supabase Storage (gotiate_assets/icons/), one
-- renamed. Same pattern as 20260817220000_theme_entity_content_update.sql
-- -- see that migration's own comment and DATABASE.md for why a direct
-- content UPDATE here is the sanctioned convention, not a one-off.
--
-- theme_data/fictional_companies_v1.json (the offline test suite's
-- fixture -- see AGENTS.md on why tests never touch this table) carries
-- the same five changes as of this same commit, kept in sync by hand.

update theme_entities set logo_url = 'https://vomnvmnsdhvwtqkjtoal.supabase.co/storage/v1/object/public/gotiate_assets/icons/ham.png'
  where theme_set_id = 'fictional_companies_v1' and theme_set_version = 1 and theme_key = 'fc_006';

update theme_entities set logo_url = 'https://vomnvmnsdhvwtqkjtoal.supabase.co/storage/v1/object/public/gotiate_assets/icons/meisterburger.png'
  where theme_set_id = 'fictional_companies_v1' and theme_set_version = 1 and theme_key = 'fc_011';

update theme_entities set logo_url = 'https://vomnvmnsdhvwtqkjtoal.supabase.co/storage/v1/object/public/gotiate_assets/icons/scuttleworth.png'
  where theme_set_id = 'fictional_companies_v1' and theme_set_version = 1 and theme_key = 'fc_027';

update theme_entities set logo_url = 'https://vomnvmnsdhvwtqkjtoal.supabase.co/storage/v1/object/public/gotiate_assets/icons/mining.png'
  where theme_set_id = 'fictional_companies_v1' and theme_set_version = 1 and theme_key = 'fc_028';

update theme_entities set display_name = 'Oxide Tractor Co.'
  where theme_set_id = 'fictional_companies_v1' and theme_set_version = 1 and theme_key = 'fc_031';
