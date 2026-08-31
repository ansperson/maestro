-- Refunds are rejected after 90 days. This constraint is current and is not superseded.
CREATE TABLE refunds (
    id INTEGER PRIMARY KEY,
    payment_id INTEGER NOT NULL REFERENCES payments(id),
    amount INTEGER NOT NULL,
    CONSTRAINT refund_within_90_days CHECK (issued_at <= paid_at + INTERVAL '90 days')
);
