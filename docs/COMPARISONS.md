# Comparisons

SemanticSQL is not a BI suite and not a chat UI. It is the query engine layer:
app language in, scoped SQL out, with diagnostics when the answer should not
run.

## Vanna

[Vanna](https://github.com/vanna-ai/vanna) proved the shape of the demand: ask a
database a question, get SQL, table output, charts, and a summary. Its public
repo is now archived/read-only, and the open issue list captures the problems a
production NL-to-SQL engine has to take seriously.

Relevant open issues:

- [#1121: SQL Injection vulnerability in `remove_training_data` (CVE-2026-4229)](https://github.com/vanna-ai/vanna/issues/1121)
- [#1098: SQL injection in Databricks/BigQuery vector stores + unsafe `exec()` on LLM output](https://github.com/vanna-ai/vanna/issues/1098)
- [#1078: Remote Code Execution report](https://github.com/vanna-ai/vanna/issues/1078)
- [#1062: Prompt injection to RCE via generated visualization code](https://github.com/vanna-ai/vanna/issues/1062)
- [#1103: Wrong SQL auto-saved to tool memory](https://github.com/vanna-ai/vanna/issues/1103)
- [#1105: Too much process data displayed to users](https://github.com/vanna-ai/vanna/issues/1105)

SemanticSQL's defense is architectural:

- no direct execution of LLM-authored SQL;
- no generated Python/chart code execution;
- no automatic memory save just because a query executed;
- default query path emits SQL only after graph grounding, scoping, rendering,
  sqlglot validation, and Rust second-pass parsing;
- diagnostic detail is opt-in through `--query-frame-json`, not a default user
  output stream;
- row dumps and unsafe actions can fail closed.

That does not mean "no bugs." It means the dangerous behavior is not on the
happy path.

## microsoft/NaturalLanguageToSQL

[microsoft/NaturalLanguageToSQL](https://github.com/microsoft/NaturalLanguageToSQL)
is a useful demo repo. Its README describes three variants: Phi-3 serverless,
local Phi-3 through Ollama, and Azure OpenAI/GPT-4. The example code prompts a
model to generate SQL and then executes the generated query.

SemanticSQL is built as a governed runtime instead of a model-call demo:

| Area | microsoft/NaturalLanguageToSQL | SemanticSQL |
| --- | --- | --- |
| Shape | Three example scripts | CLI/runtime + graph + validators |
| Published benchmark | None in repo | Pending cleaned-runtime rerun |
| Grounded local route | Model call per question | QueryFrame can route grounded questions locally |
| Safety | Prompt rules such as no `INSERT`, `UPDATE`, `DELETE` | SQL rendering, tenant scoping, sqlglot validation, Rust second-pass parser |
| Fail-closed evidence | None published | Cleaned-runtime QueryFrame canary `144/144` routed, `18/18` rejected fail-closed |
| Broad governed BI | Not established in repo | In progress; complex BI should route through schema-derived frames or typed LLM proposals |

The honest claim: SemanticSQL has stronger runtime-governance machinery than a
demo that directly asks a model for SQL. For grounded QueryFrame routes, it also
avoids the remote/model round trip. It is not yet a full BI product like Defog
or ThoughtSpot; ratio formulas, anti-join analytics, saved metrics, dashboards,
and user-facing visualization workflows remain separate product work.

## Paid Alternatives

| Product | Public cost signal | What you are buying |
| --- | --- | --- |
| SemanticSQL | Open source runtime; self-hosted costs are your infra/LLM choices | The query engine layer inside your app |
| [Vanna Cloud](https://vanna.ai/pricing) | Explorer `$50/month`, Team `$500/month`, Enterprise custom | Chat/data insight product with hosted options |
| [Defog](https://defog.ai/pricing) | Enterprise Cloud Hosted `$5,000/month` | Enterprise AI analyst platform |
| [ThoughtSpot](https://www.thoughtspot.com/pricing) | Pro starts at `$50/user/month`; Enterprise custom | Full BI/search/AI analytics platform |

Third-party spend data from
[SpendHound](https://www.spendhound.com/marketplace/thoughtspot-pricing)
reports average ThoughtSpot enterprise spend around `$255,768/year`, which is
useful context when the actual need is "let users ask questions inside the
product we already have."

## Bottom Line

If you need a full BI suite, buy a BI suite.

If you need a safe NL-to-SQL core inside an app you already own, SemanticSQL is
aimed at that narrower job.
