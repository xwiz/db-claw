-- Seed for the end-to-end cascade test. Two entities, one enum.

INSERT INTO semsql_metadata (key, value) VALUES ('schema_version', '1');

INSERT INTO entities (canonical_name, db_table, db_schema, singular_label, plural_label, proto_blob)
VALUES ('users',   'users',   'public', 'Student',      'Students',      X'');

INSERT INTO entities (canonical_name, db_table, db_schema, singular_label, plural_label, proto_blob)
VALUES ('tenants', 'tenants', 'public', 'Organization', 'Organizations', X'');

INSERT INTO fields (entity, field, db_column, type, display_label, enum_canonical, unit_canonical, proto_blob)
VALUES ('users', 'status_code', 'status_code', 'integer', 'Status', 'users.status_code', NULL, X'');

INSERT INTO fields (entity, field, db_column, type, display_label, enum_canonical, unit_canonical, proto_blob)
VALUES ('users', 'created_at', 'created_at', 'timestamp', 'Joined Date', NULL, NULL, X'');

-- Numeric field for comparison + ordering + Top-N patterns.
INSERT INTO fields (entity, field, db_column, type, display_label, enum_canonical, unit_canonical, proto_blob)
VALUES ('users', 'balance', 'balance', 'INTEGER', 'Account Balance', NULL, NULL, X'');

INSERT INTO enums (canonical_name, proto_blob, _enum_values_json)
VALUES ('users.status_code', X'', '{"1": "Pending", "2": "Active", "39": "Error"}');
