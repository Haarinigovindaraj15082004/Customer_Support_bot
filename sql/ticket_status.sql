UPDATE tickets
SET status = 'closed',
    updated_utc = datetime('now')
WHERE id BETWEEN 1 AND 20;

UPDATE tickets
SET status = 'open',
    updated_utc = datetime('now')
WHERE id BETWEEN 21 AND 31;

UPDATE tickets
SET status = 'in_progress',
    updated_utc = datetime('now')
WHERE id BETWEEN 32 AND 42;

