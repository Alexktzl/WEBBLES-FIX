fn consume(s: String) { println!("{}", s); }
fn main() {
    let buffer = String::from("x");
    consume(buffer.clone());
    consume(buffer);
}
