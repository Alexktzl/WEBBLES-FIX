fn consume(s: String) { println!("{}", s); }
fn main() {
    let cursor = String::from("x");
    consume(cursor);
    consume(cursor);
}
