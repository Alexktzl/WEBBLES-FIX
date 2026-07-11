fn consume(s: String) { println!("{}", s); }
fn main() {
    let temp = String::from("x");
    consume(temp.clone());
    consume(temp);
}
