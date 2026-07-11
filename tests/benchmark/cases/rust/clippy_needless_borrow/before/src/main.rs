fn show(s: &str) { println!("{}", s); }
fn main() {
    let name = String::from("Alex");
    show(&&name);
}
