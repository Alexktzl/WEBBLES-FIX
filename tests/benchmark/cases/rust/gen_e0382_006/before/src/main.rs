fn consume(s: String) { println!("{}", s); }
fn main() {
    let offset = String::from("x");
    consume(offset);
    consume(offset);
}
