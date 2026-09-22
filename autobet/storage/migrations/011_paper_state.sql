ALTER TABLE bets DROP CONSTRAINT bets_state_check;
ALTER TABLE bets ADD CONSTRAINT bets_state_check CHECK (state IN (
    'placed', 'paper', 'refused', 'error'
));
UPDATE bets SET state = 'paper' WHERE refusal_code IN ('paper_mode', 'dry_run');
