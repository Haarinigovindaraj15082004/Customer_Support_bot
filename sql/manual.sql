CREATE TABLE IF NOT EXISTS manual (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product TEXT NOT NULL,           
            section TEXT NOT NULL,           
            markdown TEXT NOT NULL,         
            updated_utc TEXT DEFAULT (datetime('now')),
            UNIQUE(product, section)
        )