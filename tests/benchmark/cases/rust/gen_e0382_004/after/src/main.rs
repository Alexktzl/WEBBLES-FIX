fn consume(s: String) { println!("{}", s); }
fn main() {
    let cache = String::from("x");
    consume(cache.clone());
    consume(cache);
}
