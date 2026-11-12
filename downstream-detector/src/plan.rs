use serde_json::{json, Value};
use std::{collections::BTreeMap, error::Error};

pub struct RankedPlan {
    pub eligible_repositories: usize,
    pub records_without_https_repository: usize,
    pub repositories: Vec<(String, u64, Vec<String>)>,
}

// Deduplicate registry entries by repository, but preserve the crates covered by
// each repository. Download counts are registry signals, not business impact.
pub fn make_plan(input: &str, limit: usize) -> Result<String, Box<dyn Error>> {
    let plan = rank(input)?;
    let selected: Vec<_> = plan
        .repositories
        .iter()
        .take(limit)
        .enumerate()
        .map(|(i, (repository, downloads, crates))| {
            json!({
                "rank": i + 1, "repository": repository, "downloads": downloads,
                "crates": crates, "revision": null,
                "note": "Confirm an immutable revision and repository permission before testing."
            })
        })
        .collect();
    Ok(serde_json::to_string_pretty(
        &json!({"upstream": serde_json::from_str::<Value>(input)?["upstream"], "limit": limit, "eligible_repositories": plan.eligible_repositories, "records_without_https_repository": plan.records_without_https_repository, "selected": selected}),
    )?)
}

pub fn rank(input: &str) -> Result<RankedPlan, Box<dyn Error>> {
    let input: Value = serde_json::from_str(input)?;
    let deps = input["dependents"]
        .as_array()
        .ok_or("missing dependents array")?;
    let mut repos: BTreeMap<String, (u64, Vec<String>)> = BTreeMap::new();
    let mut skipped = 0usize;
    for dep in deps {
        let Some(repo) = dep["repository"]
            .as_str()
            .filter(|r| r.starts_with("https://"))
        else {
            skipped += 1;
            continue;
        };
        let repo = repo
            .trim_end_matches('/')
            .trim_end_matches(".git")
            .to_string();
        let name = dep["crate"].as_str().ok_or("missing crate name")?;
        let entry = repos.entry(repo).or_default();
        entry.0 = entry.0.max(dep["downloads"].as_u64().unwrap_or(0));
        if !entry.1.iter().any(|n| n == name) {
            entry.1.push(name.to_string());
        }
    }
    let mut rows: Vec<_> = repos.into_iter().collect();
    rows.sort_by(|a, b| b.1 .0.cmp(&a.1 .0).then(a.0.cmp(&b.0)));
    let count = rows.len();
    Ok(RankedPlan {
        eligible_repositories: count,
        records_without_https_repository: skipped,
        repositories: rows
            .into_iter()
            .map(|(repository, (downloads, crates))| (repository, downloads, crates))
            .collect(),
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn ranks_deduplicates_and_respects_limit() {
        let input = r#"{"upstream":"x","dependents":[{"crate":"a","repository":"https://example/a","downloads":2},{"crate":"b","repository":"https://example/b","downloads":9},{"crate":"c","repository":"https://example/b.git","downloads":3},{"crate":"d","repository":null}]}"#;
        let plan: Value = serde_json::from_str(&make_plan(input, 1).unwrap()).unwrap();
        assert_eq!(plan["eligible_repositories"], 2);
        assert_eq!(plan["records_without_https_repository"], 1);
        assert_eq!(plan["selected"][0]["repository"], "https://example/b");
        assert_eq!(plan["selected"][0]["crates"].as_array().unwrap().len(), 2);
    }
}
