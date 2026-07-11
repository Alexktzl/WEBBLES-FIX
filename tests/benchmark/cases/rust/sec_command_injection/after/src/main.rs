use std::process::Command;

fn run(user_input: &str) {
    Command::new("echo").arg(user_input).status().unwrap();
}
