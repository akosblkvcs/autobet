UPDATE bets SET state = 'paper' WHERE refusal_code IN ('paper_mode', 'dry_run');
