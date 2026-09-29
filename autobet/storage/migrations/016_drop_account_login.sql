ALTER TABLE bookmaker_accounts
    DROP COLUMN IF EXISTS last_login_at,
    DROP COLUMN IF EXISTS last_error;
