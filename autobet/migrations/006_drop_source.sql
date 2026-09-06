ALTER TABLE messages DROP COLUMN source;
ALTER TABLE messages ADD PRIMARY KEY (external_id);
