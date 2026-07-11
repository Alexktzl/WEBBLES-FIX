fn consume(s: String) { println!("{}", s); }
fn main() {
    let counter = String::from("x");
    consume(counter.clone());
    consume(counter);
}
