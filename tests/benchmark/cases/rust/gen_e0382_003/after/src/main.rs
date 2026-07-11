fn consume(s: String) { println!("{}", s); }
fn main() {
    let result = String::from("x");
    consume(result.clone());
    consume(result);
}
