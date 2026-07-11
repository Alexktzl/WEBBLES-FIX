fn consume(s: String) { println!("{}", s); }
fn main() {
    let handle = String::from("x");
    consume(handle);
    consume(handle);
}
