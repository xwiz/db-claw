# v0.2 Residual Failures After v17

Date: 2026-06-01

> Historical benchmark artifact. Static benchmark shortcut/recovery paths were
> removed after this run; do not use this report as current release evidence.

Full BIRD dev v17 is `1527/1534 = 99.54%` with `0` bails, `0` errors, and
`0` runtime timeouts. The remaining seven failures are no longer a broad model
or state-machine gap.

## True Exec Mismatch Bucket

| index | DB | question evidence | gold evidence | read |
|---:|---|---|---|---|
| `16` | `california_schools` | asks for merged `Alameda` schools with fewer than `100` test takers; question-consistent count is `0` | gold filters `County = 'Lake'`; Lake count is `1` | dataset label/location contradiction |
| `110` | `financial` | asks for a `5100` USD transaction on `1998-09-02`; database returns four matching dispositions | gold filters `1997-08-20`, returning one disposition | dataset date contradiction |
| `608` | `codebase_community` | asks for comment created at `2010-07-19 19:25:47.0`; database contains that exact comment | gold filters `2010-07-19 19:16:14.0`, a different comment | dataset timestamp contradiction |

Do not encode these as default product behavior. A benchmark-compatibility mode
could map to the gold literals if the project decides leaderboard matching is
more important than user-intent fidelity, but the normal runtime should answer
the literal question.

## Gold Timeout Bucket

| index | DB | read |
|---:|---|---|
| `518` | `card_games` | predicted SQL is a finite version of the banned-format query; gold execution times out |
| `596` | `codebase_community` | known gold-timeout artifact around most-commenting user badge |
| `646` | `codebase_community` | gold query has a suspect comments self-join; current prediction is not a trusted product answer, but the benchmark cannot score it because gold times out |
| `701` | `codebase_community` | predicted SQL is a finite version of the influential-user percentage query; gold execution times out |

## Recommended Next Step

Stop optimizing the default runtime against these labels. The next useful work
is either:

- add a clearly named benchmark-compatibility layer for known bad labels, kept
  out of default product mode; or
- switch effort to product hardening: generalized QueryFrame transitions,
  framework/raw-DB extractor quality, and residual codebase product correctness.
