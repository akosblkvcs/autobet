-- One carrier, so the column held 'telegram' on every row and carried no
-- information. Dropping it takes the composite key with it; external_id is
-- already unique on its own, being "{chat_id}:{message_id}".
ALTER TABLE messages DROP COLUMN source;
ALTER TABLE messages ADD PRIMARY KEY (external_id);
