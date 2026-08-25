CREATE TABLE payments (
    id INTEGER PRIMARY KEY,
    order_id INTEGER NOT NULL REFERENCES orders(id)
);
CREATE INDEX payments_order_id_idx ON payments(order_id);
