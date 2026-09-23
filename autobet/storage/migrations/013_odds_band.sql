ALTER TABLE user_settings ADD COLUMN max_odds_rise_percent double precision;
UPDATE settings SET key = 'max_odds_rise_percent' WHERE key = 'mismatch_rise_percent';

DELETE FROM settings WHERE key IN ('max_odds_drop_percent', 'max_odds_rise_percent');
