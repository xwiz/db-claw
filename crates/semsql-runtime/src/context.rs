//! Authored semantic-contract and approved resolution-memory overlays.

use crate::{APPROVED_MEMORY_SOURCE_LAYER, AUTHORED_CONTRACT_SOURCE_LAYER};
use semsql_core::{Result, SemsqlError};
use semsql_graph::read::{metadata, MetricDefinitionRow, RelationshipRow, VocabularyEntry};
use serde::{Deserialize, Serialize};
use std::path::Path;

/// Context loaded beside a generated SemanticGraph.
#[derive(Clone, Debug, Default)]
pub struct SemanticContextOverlay {
    /// Authored and approved-memory vocabulary.
    pub vocabulary: Vec<VocabularyEntry>,
    /// Authored governed metrics.
    pub metric_definitions: Vec<MetricDefinitionRow>,
    /// Authored and approved-memory relationships.
    pub relationships: Vec<RelationshipRow>,
    /// Number of authored aliases loaded.
    pub authored_alias_count: usize,
    /// Number of approved memory entries loaded.
    pub approved_memory_count: usize,
    /// Number of memory entries ignored because their drift key was stale.
    pub stale_memory_count: usize,
}

/// Source-controlled authored semantic contract.
#[derive(Clone, Debug, Default, Deserialize, Serialize)]
pub struct SemanticContract {
    #[serde(default = "schema_version_one")]
    /// Contract schema version.
    pub schema_version: u64,
    #[serde(default)]
    /// Reviewed aliases.
    pub aliases: Vec<SemanticAlias>,
    #[serde(default)]
    /// Governed metric definitions.
    pub metrics: Vec<SemanticMetric>,
}

/// One reviewed natural-language alias.
#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct SemanticAlias {
    /// Natural-language phrase.
    pub term: String,
    /// Canonical target kind.
    pub kind: String,
    /// Canonical entity, field, value, relationship, or scope target.
    pub target: String,
    #[serde(default = "default_authored_confidence")]
    /// Reviewed confidence in `[0, 1]`.
    pub confidence: f32,
}

/// One authored governed metric.
#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct SemanticMetric {
    /// Stable metric name.
    pub name: String,
    #[serde(default)]
    /// User-facing label.
    pub display_label: Option<String>,
    /// `conditional_rate` or `aggregate`.
    pub metric_kind: String,
    /// Metric subject entity.
    pub subject_entity: String,
    #[serde(default)]
    /// Conditional-rate numerator field.
    pub numerator_field: String,
    #[serde(default = "default_equals")]
    /// Conditional-rate numerator operator.
    pub numerator_operator: String,
    #[serde(default)]
    /// Conditional-rate numerator value.
    pub numerator_value: String,
    #[serde(default = "default_metric_evidence")]
    /// Numerator evidence kind.
    pub numerator_value_kind: String,
    #[serde(default)]
    /// Conditional-rate denominator field.
    pub denominator_field: String,
    #[serde(default = "default_scale")]
    /// Metric scale.
    pub scale: f64,
    #[serde(default)]
    /// Aggregate measure field.
    pub measure_field: Option<String>,
    #[serde(default)]
    /// Aggregate function.
    pub aggregate: Option<String>,
    #[serde(default)]
    /// Whether the measure is distinct.
    pub distinct: bool,
    #[serde(default)]
    /// Required entities.
    pub required_entities: Vec<String>,
    #[serde(default)]
    /// User-facing metric aliases.
    pub aliases: Vec<String>,
}

/// Mutable resolution-memory sidecar.
#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct ResolutionMemory {
    #[serde(default = "schema_version_one")]
    /// Memory schema version.
    pub schema_version: u64,
    #[serde(default)]
    /// Default graph/application drift key.
    pub drift_key: Option<String>,
    #[serde(default)]
    /// Explicitly reviewed resolution entries.
    pub entries: Vec<ResolutionMemoryEntry>,
}

impl Default for ResolutionMemory {
    fn default() -> Self {
        Self {
            schema_version: 1,
            drift_key: None,
            entries: Vec::new(),
        }
    }
}

/// One learned or governed resolution.
#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct ResolutionMemoryEntry {
    /// Natural-language phrase.
    pub term: String,
    /// Canonical target kind.
    pub kind: String,
    /// Canonical target.
    pub target: String,
    /// `provisional`, `confirmed`, `governed`, `rejected`, or `stale`.
    pub status: String,
    #[serde(default = "default_memory_confidence")]
    /// Confidence in `[0, 1]`.
    pub confidence: f32,
    #[serde(default)]
    /// Entry-specific drift key.
    pub drift_key: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    /// Optional provenance payload.
    pub provenance: Option<serde_json::Value>,
}

/// Load an optional authored contract and optional approved-memory sidecar.
pub fn load(
    graph_path: &Path,
    contract_path: Option<&Path>,
    memory_path: Option<&Path>,
) -> Result<SemanticContextOverlay> {
    let mut overlay = SemanticContextOverlay::default();
    if let Some(path) = contract_path {
        let contract: SemanticContract = parse_yaml_or_json(path)?;
        ensure_schema_version(contract.schema_version, path)?;
        for alias in contract.aliases {
            validate_alias(&alias.kind, &alias.target, alias.confidence)?;
            overlay.vocabulary.push(VocabularyEntry {
                term: alias.term.trim().to_ascii_lowercase(),
                canonical_kind: alias.kind,
                canonical_value: alias.target,
                confidence: alias.confidence,
                source_layer: AUTHORED_CONTRACT_SOURCE_LAYER,
            });
            overlay.authored_alias_count += 1;
        }
        for metric in contract.metrics {
            overlay.metric_definitions.push(metric.into_row()?);
        }
    }
    if let Some(path) = memory_path {
        let memory: ResolutionMemory = parse_yaml_or_json(path)?;
        ensure_schema_version(memory.schema_version, path)?;
        let graph_drift_key = metadata(graph_path, "schema_hash")?;
        for entry in memory.entries {
            let effective_drift_key = entry.drift_key.as_ref().or(memory.drift_key.as_ref());
            let stale =
                effective_drift_key.is_some() && graph_drift_key.as_ref() != effective_drift_key;
            if stale {
                overlay.stale_memory_count += 1;
                continue;
            }
            if !matches!(entry.status.as_str(), "confirmed" | "governed") {
                continue;
            }
            validate_alias(&entry.kind, &entry.target, entry.confidence)?;
            let term = entry.term.trim().to_ascii_lowercase();
            let target = entry.target;
            if entry.kind == "relationship" {
                let relationship = parse_relationship_target(&target)?;
                overlay.relationships.push(relationship);
            }
            overlay.vocabulary.push(VocabularyEntry {
                term,
                canonical_kind: entry.kind,
                canonical_value: target,
                confidence: entry.confidence,
                source_layer: APPROVED_MEMORY_SOURCE_LAYER,
            });
            overlay.approved_memory_count += 1;
        }
    }
    Ok(overlay)
}

fn parse_relationship_target(target: &str) -> Result<RelationshipRow> {
    let (left, right) = target.split_once("->").ok_or_else(|| {
        SemsqlError::Other(format!(
            "relationship memory target must use `left -> right` syntax, got `{target}`"
        ))
    })?;
    let (from_entity, from_field) = parse_relationship_endpoint(left.trim())?;
    let (to_entity, to_field) = parse_relationship_endpoint(right.trim())?;
    Ok(RelationshipRow {
        from_entity,
        from_field,
        to_entity,
        to_field,
        kind: "approved_memory".to_string(),
    })
}

fn parse_relationship_endpoint(endpoint: &str) -> Result<(String, String)> {
    let (entity, field) = endpoint.split_once('.').ok_or_else(|| {
        SemsqlError::Other(format!(
            "relationship memory endpoint must use `entity.field` syntax, got `{endpoint}`"
        ))
    })?;
    if entity.trim().is_empty() || field.trim().is_empty() {
        return Err(SemsqlError::Other(format!(
            "relationship memory endpoint must not contain empty parts, got `{endpoint}`"
        )));
    }
    Ok((entity.trim().to_string(), field.trim().to_string()))
}

fn parse_yaml_or_json<T: for<'de> Deserialize<'de>>(path: &Path) -> Result<T> {
    let text = std::fs::read_to_string(path).map_err(|e| {
        SemsqlError::Other(format!("read semantic context `{}`: {e}", path.display()))
    })?;
    serde_yaml::from_str(&text).map_err(|e| {
        SemsqlError::Other(format!(
            "parse semantic context `{}` as YAML/JSON: {e}",
            path.display()
        ))
    })
}

fn ensure_schema_version(version: u64, path: &Path) -> Result<()> {
    if version != 1 {
        return Err(SemsqlError::Other(format!(
            "unsupported semantic context schema_version {version} in `{}`",
            path.display()
        )));
    }
    Ok(())
}

fn validate_alias(kind: &str, target: &str, confidence: f32) -> Result<()> {
    if !matches!(
        kind,
        "entity" | "field" | "enum_value" | "relationship" | "scope_predicate"
    ) {
        return Err(SemsqlError::Other(format!(
            "unsupported semantic alias kind `{kind}`"
        )));
    }
    if target.trim().is_empty() {
        return Err(SemsqlError::Other(
            "semantic alias target must not be empty".to_string(),
        ));
    }
    if !(0.0..=1.0).contains(&confidence) {
        return Err(SemsqlError::Other(format!(
            "semantic alias confidence must be in [0, 1], got {confidence}"
        )));
    }
    Ok(())
}

impl SemanticMetric {
    fn into_row(self) -> Result<MetricDefinitionRow> {
        if !matches!(self.metric_kind.as_str(), "conditional_rate" | "aggregate") {
            return Err(SemsqlError::Other(format!(
                "unsupported semantic metric kind `{}`",
                self.metric_kind
            )));
        }
        Ok(MetricDefinitionRow {
            name: self.name,
            display_label: self.display_label,
            metric_kind: self.metric_kind,
            subject_entity: self.subject_entity,
            numerator_field: self.numerator_field,
            numerator_operator: self.numerator_operator,
            numerator_value: self.numerator_value,
            numerator_value_kind: self.numerator_value_kind,
            denominator_field: self.denominator_field,
            scale: self.scale,
            measure_field: self.measure_field,
            aggregate: self.aggregate.map(|value| value.to_ascii_uppercase()),
            distinct_measure: self.distinct,
            required_entities: self.required_entities,
            aliases: self.aliases,
        })
    }
}

const fn schema_version_one() -> u64 {
    1
}

fn default_authored_confidence() -> f32 {
    1.0
}

fn default_memory_confidence() -> f32 {
    0.9
}

fn default_equals() -> String {
    "=".to_string()
}

fn default_metric_evidence() -> String {
    "semantic_contract".to_string()
}

fn default_scale() -> f64 {
    1.0
}

#[cfg(test)]
mod tests {
    use super::*;
    use semsql_graph::write::stamp_metadata;

    #[test]
    fn loads_authored_contract_and_only_approved_fresh_memory() {
        let dir = tempfile::tempdir().unwrap();
        let graph = dir.path().join("app.semsql");
        let connection = semsql_graph::open(&graph).unwrap();
        stamp_metadata(&connection, "test-app", "schema-123").unwrap();
        let contract = dir.path().join("semsql.contract.yaml");
        std::fs::write(
            &contract,
            r#"
schema_version: 1
aliases:
  - term: customer owner
    kind: field
    target: accounts.owner_id
metrics:
  - name: active_account_rate
    metric_kind: conditional_rate
    subject_entity: accounts
    numerator_field: accounts.status
    numerator_value: active
    denominator_field: accounts.id
    scale: 100
    aliases: [active account rate]
"#,
        )
        .unwrap();
        let memory = dir.path().join("semsql.memory.yaml");
        std::fs::write(
            &memory,
            r#"
schema_version: 1
drift_key: schema-123
entries:
  - term: speed
    kind: field
    target: transactions.speed_score
    status: confirmed
  - term: risky
    kind: field
    target: transactions.risk_score
    status: provisional
"#,
        )
        .unwrap();

        let overlay = load(&graph, Some(&contract), Some(&memory)).unwrap();

        assert_eq!(overlay.authored_alias_count, 1);
        assert_eq!(overlay.approved_memory_count, 1);
        assert_eq!(overlay.stale_memory_count, 0);
        assert_eq!(overlay.metric_definitions.len(), 1);
        assert!(overlay.vocabulary.iter().any(|entry| entry.term == "speed"));
        assert!(!overlay.vocabulary.iter().any(|entry| entry.term == "risky"));
    }

    #[test]
    fn loads_approved_relationship_memory_as_relationship_edge() {
        let dir = tempfile::tempdir().unwrap();
        let graph = dir.path().join("app.semsql");
        let connection = semsql_graph::open(&graph).unwrap();
        stamp_metadata(&connection, "test-app", "schema-123").unwrap();
        let memory = dir.path().join("semsql.memory.yaml");
        std::fs::write(
            &memory,
            r#"
schema_version: 1
drift_key: schema-123
entries:
  - term: bank
    kind: relationship
    target: bank_settings.bank_id -> banks.id
    status: confirmed
"#,
        )
        .unwrap();

        let overlay = load(&graph, None, Some(&memory)).unwrap();

        assert_eq!(overlay.approved_memory_count, 1);
        assert_eq!(overlay.relationships.len(), 1);
        let relationship = &overlay.relationships[0];
        assert_eq!(relationship.from_entity, "bank_settings");
        assert_eq!(relationship.from_field, "bank_id");
        assert_eq!(relationship.to_entity, "banks");
        assert_eq!(relationship.to_field, "id");
        assert_eq!(relationship.kind, "approved_memory");
        assert!(overlay.vocabulary.iter().any(|entry| entry.term == "bank"));
    }

    #[test]
    fn stale_memory_is_reported_and_not_loaded() {
        let dir = tempfile::tempdir().unwrap();
        let graph = dir.path().join("app.semsql");
        let connection = semsql_graph::open(&graph).unwrap();
        stamp_metadata(&connection, "test-app", "schema-new").unwrap();
        let memory = dir.path().join("semsql.memory.yaml");
        std::fs::write(
            &memory,
            r#"
schema_version: 1
drift_key: schema-old
entries:
  - term: speed
    kind: field
    target: transactions.speed_score
    status: governed
"#,
        )
        .unwrap();

        let overlay = load(&graph, None, Some(&memory)).unwrap();

        assert_eq!(overlay.approved_memory_count, 0);
        assert_eq!(overlay.stale_memory_count, 1);
        assert!(overlay.vocabulary.is_empty());
    }
}
