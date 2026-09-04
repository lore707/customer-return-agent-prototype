PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS return_cases (
    id TEXT PRIMARY KEY,
    session_id TEXT,
    shopify_order_id TEXT,
    shopify_order_number TEXT,
    customer_id TEXT,
    customer_name TEXT,
    customer_email TEXT,
    product_name TEXT,
    sku TEXT,
    variant_id TEXT,
    line_item_id TEXT,
    quantity INTEGER,
    purchase_date TEXT,
    delivery_date TEXT,
    request_date TEXT NOT NULL,
    return_type TEXT NOT NULL,
    return_reason TEXT NOT NULL,
    detailed_reason TEXT,
    customer_message TEXT NOT NULL,
    ai_classification TEXT NOT NULL,
    confidence REAL,
    eligibility_result TEXT NOT NULL,
    policy_applied TEXT,
    policy_decision TEXT NOT NULL DEFAULT '{}',
    suggested_resolution TEXT NOT NULL,
    original_suggested_response TEXT,
    final_response TEXT,
    human_decision TEXT,
    human_reason TEXT,
    label_status TEXT NOT NULL DEFAULT 'not_created',
    shipping_provider TEXT,
    sendcloud_return_id TEXT,
    tracking_number TEXT,
    label_url TEXT,
    refund_status TEXT NOT NULL DEFAULT 'not_started',
    replacement_status TEXT NOT NULL DEFAULT 'not_started',
    status TEXT NOT NULL,
    assigned_operator TEXT,
    analysis_duration_ms INTEGER,
    operator_decision_seconds INTEGER,
    api_action_count INTEGER NOT NULL DEFAULT 0,
    manual_step_count INTEGER NOT NULL DEFAULT 0,
    data_source TEXT,
    source_mode TEXT,
    source_fetched_at TEXT,
    source_payload TEXT NOT NULL DEFAULT '{}',
    customer_history TEXT NOT NULL DEFAULT '{}',
    evidence TEXT NOT NULL DEFAULT '{}',
    integration_state TEXT NOT NULL DEFAULT '{}',
    replacement_order_number TEXT,
    guided_step INTEGER NOT NULL DEFAULT 0,
    ai_mode TEXT,
    scenario_slug TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    closed_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_return_cases_status
    ON return_cases(status);
CREATE INDEX IF NOT EXISTS idx_return_cases_order
    ON return_cases(shopify_order_number);
CREATE INDEX IF NOT EXISTS idx_return_cases_customer
    ON return_cases(customer_email);
CREATE INDEX IF NOT EXISTS idx_return_cases_sku
    ON return_cases(sku);
CREATE TABLE IF NOT EXISTS audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    from_status TEXT,
    to_status TEXT,
    details TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    FOREIGN KEY(case_id) REFERENCES return_cases(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_audit_events_case
    ON audit_events(case_id, created_at, id);

CREATE TABLE IF NOT EXISTS case_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id TEXT,
    session_id TEXT NOT NULL,
    customer_id TEXT,
    customer_email TEXT,
    role TEXT NOT NULL,
    channel TEXT NOT NULL DEFAULT 'dashboard',
    message_type TEXT NOT NULL DEFAULT 'message',
    message TEXT NOT NULL,
    metadata TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    FOREIGN KEY(case_id) REFERENCES return_cases(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_case_messages_case
    ON case_messages(case_id, created_at, id);
CREATE INDEX IF NOT EXISTS idx_case_messages_session
    ON case_messages(session_id, created_at, id);
CREATE INDEX IF NOT EXISTS idx_case_messages_customer
    ON case_messages(customer_id, customer_email, created_at);

CREATE TABLE IF NOT EXISTS operator_feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id TEXT NOT NULL,
    category TEXT,
    feedback_type TEXT NOT NULL,
    reason_tag TEXT,
    instructions TEXT,
    original_draft TEXT,
    revised_draft TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY(case_id) REFERENCES return_cases(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_operator_feedback_case
    ON operator_feedback(case_id, created_at, id);
CREATE INDEX IF NOT EXISTS idx_operator_feedback_category
    ON operator_feedback(category, reason_tag, created_at);
