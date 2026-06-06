# Real DB MySQL/MariaDB Schema-Only Probe

- status: `PASS`
- database: `cloudspace_kyc_dashboard`
- graph: `target\realdb_schema_probe_cloudspace_cleaned_v2\graphs\cloudspace_kyc_dashboard.schemaonly.semsql`
- high-risk schema: `True`
- safety mode: `schema-only extraction; count-only execution; no result values retained`

## Summary

- questions: `12`
- routed: `10`
- count-only routes: `10`
- executed count-only queries: `10`
- execution errors: `0`
- safe not-executed routes/rejects: `2`
- semantic ok or safe not-executed: `12/12`
- needs review: `0`
- sample-value rows: `0`
- stages: `{'stage_0a': 10, 'needs_model': 2}`

## Records

| # | Question | Stage | Expected | Actual | Count-only | Executed | Exec | Review | SQL |
|---:|---|---|---|---|---:|---:|---:|---|---|
| 1 | `how many contact us` | `stage_0a` | `contact_us` | `contact_us` | `True` | `True` | `ok` | `ok` | <code>SELECT COUNT(*) FROM `contact_us`</code> |
| 2 | `how many migrations` | `stage_0a` | `migrations` | `migrations` | `True` | `True` | `ok` | `ok` | <code>SELECT COUNT(*) FROM `migrations`</code> |
| 3 | `how many notifications` | `stage_0a` | `notifications` | `notifications` | `True` | `True` | `ok` | `ok` | <code>SELECT COUNT(*) FROM `notifications`</code> |
| 4 | `how many password reset tokens` | `stage_0a` | `password_reset_tokens` | `password_reset_tokens` | `True` | `True` | `ok` | `ok` | <code>SELECT COUNT(*) FROM `password_reset_tokens`</code> |
| 5 | `how many role has permissions` | `stage_0a` | `role_has_permissions` | `role_has_permissions` | `True` | `True` | `ok` | `ok` | <code>SELECT COUNT(*) FROM `role_has_permissions`</code> |
| 6 | `how many two factor authentications` | `stage_0a` | `two_factor_authentications` | `two_factor_authentications` | `True` | `True` | `ok` | `ok` | <code>SELECT COUNT(*) FROM `two_factor_authentications`</code> |
| 7 | `how many user device tokens` | `stage_0a` | `user_device_tokens` | `user_device_tokens` | `True` | `True` | `ok` | `ok` | <code>SELECT COUNT(*) FROM `user_device_tokens`</code> |
| 8 | `how many user password holders` | `stage_0a` | `user_password_holders` | `user_password_holders` | `True` | `True` | `ok` | `ok` | <code>SELECT COUNT(*) FROM `user_password_holders`</code> |
| 9 | `how many web view logins` | `stage_0a` | `web_view_logins` | `web_view_logins` | `True` | `True` | `ok` | `ok` | <code>SELECT COUNT(*) FROM `web_view_logins`</code> |
| 10 | `how many websockets statistics entries` | `stage_0a` | `websockets_statistics_entries` | `websockets_statistics_entries` | `True` | `True` | `ok` | `ok` | <code>SELECT COUNT(*) FROM `websockets_statistics_entries`</code> |
| 11 | `list old passwords` | `needs_model` | `` | `` | `False` | `False` | `query_rejected_not_executed` | `expected_not_executed` | <code></code> |
| 12 | `list old pins` | `needs_model` | `` | `` | `False` | `False` | `query_rejected_not_executed` | `expected_not_executed` | <code></code> |
