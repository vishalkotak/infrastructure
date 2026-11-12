use serde_json::{json, Value};
use std::{collections::BTreeMap, error::Error, process::Command, thread, time::Duration};

// The registry returns dependency records and a separate versions array. Join
// them on version_id: dependency.crate_id names the crate being queried, not
// the downstream package.
fn parse_page(body: &str) -> Result<(u64, Vec<Value>), Box<dyn Error>> {
    let page: Value = serde_json::from_str(body)?;
    let total = page["meta"]["total"].as_u64().ok_or("missing meta.total")?;
    let dependencies = page["dependencies"]
        .as_array()
        .ok_or("missing dependencies")?;
    let versions = page["versions"].as_array().ok_or("missing versions")?;
    let by_id: BTreeMap<u64, &Value> = versions
        .iter()
        .filter_map(|v| v["id"].as_u64().map(|id| (id, v)))
        .collect();
    let mut results = Vec::new();
    for dep in dependencies {
        let id = dep["version_id"].as_u64().ok_or("missing version_id")?;
        let version = by_id.get(&id).ok_or("dependency's version is missing")?;
        results.push(json!({
            "crate": version["crate"].as_str().ok_or("missing dependent crate name")?,
            "version": version["num"].as_str().ok_or("missing dependent version")?,
            "repository": version["repository"],
            "requirement": dep["req"].as_str().ok_or("missing dependency requirement")?,
            "kind": dep["kind"],
            "optional": dep["optional"],
            "target": dep["target"],
            "downloads": dep["downloads"],
        }));
    }
    Ok((total, results))
}

pub fn discover(crate_name: &str, user_agent: &str) -> Result<String, Box<dyn Error>> {
    if crate_name.is_empty()
        || !crate_name
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || c == '-' || c == '_')
    {
        return Err("invalid crates.io crate name".into());
    }
    if user_agent.trim().is_empty() || user_agent.contains(['\r', '\n']) {
        return Err("invalid crates.io user agent".into());
    }
    let mut all = Vec::new();
    let mut page = 1u64;
    loop {
        let url = format!("https://crates.io/api/v1/crates/{crate_name}/reverse_dependencies?per_page=100&page={page}");
        let response = Command::new("curl")
            .args([
                "--fail",
                "--silent",
                "--show-error",
                "--location",
                "--proto",
                "=https",
                "--proto-redir",
                "=https",
                "--max-time",
                "45",
                "--user-agent",
                user_agent,
                &url,
            ])
            .output()?;
        if !response.status.success() {
            return Err(format!(
                "crates.io request for page {page} failed: {}",
                String::from_utf8_lossy(&response.stderr)
            )
            .into());
        }
        let (total, results) = parse_page(std::str::from_utf8(&response.stdout)?)?;
        if results.is_empty() {
            if (all.len() as u64) < total {
                return Err(format!(
                    "incomplete discovery: received {} of {total} registry records",
                    all.len()
                )
                .into());
            }
            break;
        }
        all.extend(results);
        if (all.len() as u64) >= total {
            break;
        }
        page += 1;
        // Be considerate of the public API; never send more than one request a second.
        thread::sleep(Duration::from_secs(1));
    }
    Ok(serde_json::to_string_pretty(
        &json!({"upstream": crate_name, "scope": "direct public crates.io reverse dependencies", "count": all.len(), "dependents": all}),
    )?)
}

#[cfg(test)]
mod tests {
    use super::parse_page;
    #[test]
    fn joins_dependencies_by_version_id_not_array_position() {
        let raw = r#"{"meta":{"total":2},"dependencies":[{"version_id":2,"req":"^1","kind":"normal","optional":false,"target":null},{"version_id":1,"req":"^1","kind":"dev","optional":true,"target":null}],"versions":[{"id":1,"crate":"first","num":"1.0.0","repository":null},{"id":2,"crate":"second","num":"2.0.0","repository":"https://example.com"}]}"#;
        let (total, rows) = parse_page(raw).unwrap();
        assert_eq!(total, 2);
        assert_eq!(rows[0]["crate"], "second");
        assert_eq!(rows[1]["crate"], "first");
    }
}
