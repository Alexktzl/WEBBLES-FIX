fn consume(s: String) { println!("{}", s); }
fn main() {
    let cache = String::from("x");
    consume(cache);
    consume(cache);
}
