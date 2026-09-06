-- What each leg resolved to in the bookmaker's feed, parallel to tip's legs.
ALTER TABLE messages ADD COLUMN offers jsonb;
