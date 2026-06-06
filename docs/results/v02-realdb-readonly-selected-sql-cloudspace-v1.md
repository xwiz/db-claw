# Real DB MySQL/MariaDB Schema-Only Probe

- status: `PASS`
- database: `cloudspace_kyc_dashboard`
- graph: `target\realdb_readonly_selected_sql_cloudspace_v1\graphs\cloudspace_kyc_dashboard.schemaonly.semsql`
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
| 1 | `how many migrations` | `stage_0a` | `migrations` | `migrations` | `True` | `True` | `ok` | `ok` | <code>SELECT COUNT(*) FROM `migrations`</code> |
| 2 | `how many notifications` | `stage_0a` | `notifications` | `notifications` | `True` | `True` | `ok` | `ok` | <code>SELECT COUNT(*) FROM `notifications`</code> |
| 3 | `how many old pins` | `stage_0a` | `old_pins` | `old_pins` | `True` | `True` | `ok` | `ok` | <code>SELECT COUNT(*) FROM `old_pins`</code> |
| 4 | `how many permissions` | `stage_0a` | `permissions` | `permissions` | `True` | `True` | `ok` | `ok` | <code>SELECT COUNT(*) FROM `permissions`</code> |
| 5 | `how many require pins` | `stage_0a` | `require_pins` | `require_pins` | `True` | `True` | `ok` | `ok` | <code>SELECT COUNT(*) FROM `require_pins`</code> |
| 6 | `how many roles` | `stage_0a` | `roles` | `roles` | `True` | `True` | `ok` | `ok` | <code>SELECT COUNT(*) FROM `roles`</code> |
| 7 | `how many two factor authentications` | `stage_0a` | `two_factor_authentications` | `two_factor_authentications` | `True` | `True` | `ok` | `ok` | <code>SELECT COUNT(*) FROM `two_factor_authentications`</code> |
| 8 | `how many user device tokens` | `stage_0a` | `user_device_tokens` | `user_device_tokens` | `True` | `True` | `ok` | `ok` | <code>SELECT COUNT(*) FROM `user_device_tokens`</code> |
| 9 | `how many users` | `stage_0a` | `users` | `users` | `True` | `True` | `ok` | `ok` | <code>SELECT COUNT(*) FROM `users`</code> |
| 10 | `how many web view logins` | `stage_0a` | `web_view_logins` | `web_view_logins` | `True` | `True` | `ok` | `ok` | <code>SELECT COUNT(*) FROM `web_view_logins`</code> |
| 11 | `list old passwords` | `needs_model` | `` | `` | `False` | `False` | `query_rejected_not_executed` | `expected_not_executed` | <code></code> |
| 12 | `list old pins` | `needs_model` | `` | `` | `False` | `False` | `query_rejected_not_executed` | `expected_not_executed` | <code></code> |
