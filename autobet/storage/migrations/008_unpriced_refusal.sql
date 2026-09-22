ALTER TABLE bets DROP CONSTRAINT bets_refusal_code_check;
ALTER TABLE bets ADD CONSTRAINT bets_refusal_code_check CHECK (refusal_code IN (
    'leg_unresolved', 'event_started', 'odds_drop', 'odds_rise', 'unpriced',
    'horizon', 'no_account', 'insufficient_balance', 'user_paused',
    'daily_loss_limit', 'stake_too_small', 'paper_mode', 'dry_run',
    'book_rejected'
));
