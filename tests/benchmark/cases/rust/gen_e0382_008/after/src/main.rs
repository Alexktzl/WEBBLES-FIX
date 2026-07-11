fn consume(s: String) { println!("{}", s); }
fn main() {
    let payload = String::from("x");
    consume(payload.clone());
    consume(payload);
}
