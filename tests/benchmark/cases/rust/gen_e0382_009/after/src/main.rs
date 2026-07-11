fn consume(s: String) { println!("{}", s); }
fn main() {
    let token = String::from("x");
    consume(token.clone());
    consume(token);
}
