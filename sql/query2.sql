BEGIN TRANSACTION;

-- Recreate the table WITHOUT the 'email' column
CREATE TABLE orders_new (
    id INTEGER PRIMARY KEY,
    order_id TEXT NOT NULL,
    created_utc TEXT
    -- add any other columns you actually have, except 'email'
);

-- Copy data across (list all kept columns, same order as above)
INSERT INTO orders_new (id, order_id, created_utc)
SELECT id, order_id, created_utc
FROM orders;

-- Swap tables
DROP TABLE orders;
ALTER TABLE orders_new RENAME TO orders;

COMMIT;
