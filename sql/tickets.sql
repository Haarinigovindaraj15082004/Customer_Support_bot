ALTER TABLE tickets ADD COLUMN priority TEXT DEFAULT 'P2';
ALTER TABLE tickets ADD COLUMN first_response_utc TEXT;
ALTER TABLE tickets ADD COLUMN resolution_utc TEXT;
ALTER TABLE tickets ADD COLUMN source TEXT DEFAULT 'chat';
