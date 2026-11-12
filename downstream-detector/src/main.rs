mod batch;
mod detector;
mod discovery;
mod plan;
mod pr_check;
mod server;

use detector::{compare, Config};
use std::env;

fn main() {
    if let Err(e) = run() {
        eprintln!("error: {e}");
        std::process::exit(1);
    }
}

fn run() -> Result<(), Box<dyn std::error::Error>> {
    let args: Vec<String> = env::args().skip(1).collect();
    if args.len() == 2 && args[0] == "batch" {
        println!("{}", batch::run(&std::fs::read_to_string(&args[1])?)?);
        return Ok(());
    }
    if args.len() == 3 && args[0] == "plan" {
        let limit: usize = args[2].parse()?;
        println!(
            "{}",
            plan::make_plan(&std::fs::read_to_string(&args[1])?, limit)?
        );
        return Ok(());
    }
    if args.len() == 2 && args[0] == "discover" {
        let user_agent = env::var("CRATES_IO_USER_AGENT").map_err(|_| {
            "set CRATES_IO_USER_AGENT to an identifying name and contact URL or email"
        })?;
        println!("{}", discovery::discover(&args[1], &user_agent)?);
        return Ok(());
    }
    if args.len() == 2 && args[0] == "check" {
        let summary = pr_check::check(&args[1])?;
        println!("{summary}");
        if summary.contains("ERROR:") || summary.contains("Verdict: POTENTIAL_REGRESSION") {
            std::process::exit(2);
        }
        return Ok(());
    }
    if !(7..=8).contains(&args.len()) || (args[0] != "compare" && args[0] != "serve") {
        return Err("usage: downstream-detector check <pull-request-url> | discover <crate-name> | <compare|serve> <upstream-git> <baseline-ref> <candidate-ref> <crate-name> <downstream-git> <downstream-ref>".into());
    }
    let config = Config {
        upstream: args[1].clone(),
        baseline: args[2].clone(),
        candidate: args[3].clone(),
        crate_name: args[4].clone(),
        downstream: args[5].clone(),
        downstream_ref: args[6].clone(),
        patch_source: args.get(7).cloned(),
    };
    if args[0] == "serve" {
        server::serve(config)?;
    } else {
        println!("{}", compare(&config)?);
    }
    Ok(())
}
