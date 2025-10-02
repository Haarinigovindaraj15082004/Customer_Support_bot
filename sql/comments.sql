-- Ticket automation helpers
ALTER TABLE tickets ADD COLUMN waiting_on_customer INTEGER DEFAULT 0;
ALTER TABLE tickets ADD COLUMN last_customer_msg_utc TEXT;
ALTER TABLE tickets ADD COLUMN last_bot_msg_utc TEXT;
ALTER TABLE tickets ADD COLUMN last_reminder_utc TEXT;
ALTER TABLE tickets ADD COLUMN escalated INTEGER DEFAULT 0;

-- Optional quick flags for simple SLAs (keep it simple)
ALTER TABLE tickets ADD COLUMN first_response_utc TEXT;
ALTER TABLE tickets ADD COLUMN resolved_utc TEXT;
