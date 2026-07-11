fn consume(s: String) { println!("{}", s); }
fn main() {
    let s = String::from("hello");
    consume(s);
    consume(s);
}
